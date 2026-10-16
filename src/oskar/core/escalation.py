"""Эскалация кандидата к LLM: Scout -> перебор меню лестницей -> Trader(вето, выбор
из проходных) -> лестница на выбранных опциях -> pending_approvals + карточка.

Порядок жесткий (ст. 2): даже после «execute» Trader'а выбранная комбинация
цен проходит ПОЛНУЮ риск-лестницу заново — LLM не источник чисел, а селектор.
Не прошла — берется проходная по правилу risk/combos.rank_combos с пометкой
combo_fallback (2026-10-16, блок F).
Каждый LLM-вызов журналируется внутри агентов (analysis_log).

scout_fn/trader_fn инжектируются (тесты — стабы, прод — реальные агенты).
"""

import inspect
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from oskar.agents.budget import BudgetExceeded
from oskar.contracts.schemas import (
    ExecutionPlan,
    RiskVerdict,
    SetupCandidate,
    SetupVerdict,
    TradeDecision,
)
from oskar.core.state import RuntimeState
from oskar.risk.combos import evaluate_combos, most_advanced, rank_combos
from oskar.risk.ladder import Caps, OpenPosition, ProfileLimits, RiskInput, evaluate

logger = logging.getLogger("oskar.escalation")

# Сколько проходных комбинаций показывать Trader'у (2026-10-16, аудит F): первые по
# единому правилу выбора; длинный список размывает выбор и раздувает промпт
MAX_TRADER_COMBOS = 8


def open_positions_from_journal(journal) -> list[OpenPosition]:
    """Портфель для лестницы: open + pending (лимитка — зарезервированный риск).

    Без этого escalator видел пустой портфель и max_open_positions /
    max_total_risk_pct / no_duplicate_position не срабатывали никогда
    (ревью 2026-10-13). Журнал сверяется с биржей монитором каждую минуту."""
    rows = journal.execute(
        "SELECT symbol, side, risk_usd FROM trades WHERE status IN ('open','pending')"
    ).fetchall()
    return [
        OpenPosition(symbol=r["symbol"], side=r["side"], risk_usd=float(r["risk_usd"] or 0.0))
        for r in rows
    ]


def _accepts_combos(fn) -> bool:
    """Принимает ли trader_fn аргумент combos (2026-10-16): сигнатура расширена
    совместимо — стабы (candidate, scout_verdict) работают как раньше."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(p.name == "combos" or p.kind is p.VAR_KEYWORD for p in params)


@dataclass(frozen=True)
class EscalationResult:
    outcome: str  # scout_pass | trader_decline | risk_rejected | pending | executed | error
    scout_json: str | None = None
    trade_json: str | None = None
    detail: str = ""
    # 2026-10-16: (entry, sl, tp) комбинации, ушедшей в план (с учетом combo_fallback) —
    # counterfactual pending/executed пишется по ней, а не по лучшей из гейта цикла
    prices: tuple[float, float, float] | None = None


class Escalator:
    def __init__(
        self,
        state: RuntimeState,
        caps: Caps,
        profile: ProfileLimits,
        *,
        scout_fn,   # (candidate) -> SetupVerdict
        # (candidate, scout_verdict, combos=None) -> TradeDecision; combos — проходные
        # комбинации (ComboEval) в порядке rank_combos, не больше MAX_TRADER_COMBOS
        trader_fn,
        resolve_fn,  # (candidate, decision) -> (entry, sl, tp, entry_type)
        approval_ttl_seconds: int,
        card_sink=None,  # (candidate, verdict, scout_verdict) -> None
        open_positions_fn=lambda: [],
        equity_fn=lambda: 1000.0,
        auto_executor=None,  # AutoExecutor: в режиме «автомат» сделка ставится без нажатия
    ):
        self._state = state
        self._caps = caps
        self._profile = profile
        self._scout = scout_fn
        self._trader = trader_fn
        self._trader_takes_combos = _accepts_combos(trader_fn)
        self._resolve = resolve_fn
        self._ttl = approval_ttl_seconds
        self._card_sink = card_sink
        self._open_positions = open_positions_fn
        self._equity = equity_fn
        self._auto = auto_executor

    def _risk_context(self) -> dict:
        """Портфель, equity и флаги для лестницы — один источник для перебора меню и
        финальной проверки (2026-10-16: проходное для Trader'а = проходное в финале)."""
        return {
            "equity_usd": float(self._equity()),
            "open_positions": list(self._open_positions()),
            "safe_mode": self._state.safe_mode,
            "paused": self._state.paused,
        }

    def _check(self, candidate, confidence, entry, sl, tp, ctx) -> RiskVerdict:
        return evaluate(
            RiskInput(candidate=candidate, entry=entry, sl=sl, tp=tp,
                      confidence=confidence, **ctx),
            self._caps, self._profile,
        )

    def __call__(self, candidate: SetupCandidate) -> EscalationResult:
        try:
            scout_verdict: SetupVerdict = self._scout(candidate)
        except BudgetExceeded as e:
            logger.warning("scout budget: %s", e)
            return EscalationResult(outcome="error", detail=f"scout_budget: {e}")
        except Exception as e:  # noqa: BLE001 - CLI/парсинг: кандидат не теряется молча
            logger.error("scout failed for %s: %s", candidate.id, e)
            return EscalationResult(outcome="error", detail=f"scout: {e}")

        sj = scout_verdict.model_dump_json()
        if scout_verdict.verdict == "pass":
            return EscalationResult(outcome="scout_pass", scout_json=sj)

        # 2026-10-16 (блок F): Trader выбирал из декартова меню заведомо непроходные тройки
        # (7 из 14 «execute» срезала финальная лестница). Теперь все комбинации идут через
        # лестницу ДО него — с confidence Scout'а и теми же портфелем/equity/флагами, что у
        # финальной проверки, — и он видит только проходные.
        conf = scout_verdict.confidence
        evals = evaluate_combos(candidate, self._caps, self._profile, confidence=conf,
                                **self._risk_context())
        allowed = rank_combos(evals)  # порядок единого правила: им же идут промпт и fallback
        if not allowed:
            # ни одна не проходит: Trader не зовем (бюджет), причина — шаг самой продвинутой
            step = most_advanced(evals).failed_step or "?"
            return EscalationResult(outcome="risk_rejected", scout_json=sj,
                                    detail=f"no_passing_combo: {step}")

        try:
            if self._trader_takes_combos:
                decision: TradeDecision = self._trader(
                    candidate, scout_verdict, combos=allowed[:MAX_TRADER_COMBOS]
                )
            else:  # старая сигнатура (candidate, scout_verdict): выбор все равно проверит лестница
                decision = self._trader(candidate, scout_verdict)
        except BudgetExceeded as e:
            return EscalationResult(outcome="error", scout_json=sj,
                                    detail=f"trader_budget: {e}")
        except Exception as e:  # noqa: BLE001
            logger.error("trader failed for %s: %s", candidate.id, e)
            return EscalationResult(outcome="error", scout_json=sj, detail=f"trader: {e}")

        tj = decision.model_dump_json()
        if decision.verdict == "decline":
            logger.info("trader veto %s: %s", candidate.id, decision.decline_reason)
            return EscalationResult(outcome="trader_decline", scout_json=sj, trade_json=tj)

        # Финальная лестница остается (ст. 2): мир мог измениться, пока думал Trader
        risk_ctx = self._risk_context()
        entry, sl, tp, entry_type = self._resolve(candidate, decision)
        verdict = self._check(candidate, conf, entry, sl, tp, risk_ctx)
        fallback = ""
        if not verdict.passed:
            failed = next((s.name for s in verdict.steps if not s.passed), "?")
            # 2026-10-16: выбор Trader'а не прошел — берем проходную по единому правилу
            # (choose_combo) из того же детерминированного меню; пометка в detail и в плане
            for combo in allowed:
                v = self._check(candidate, conf, *combo.prices, risk_ctx)
                if v.passed:
                    chosen = f"{decision.entry_option_id}/{decision.sl_option_id}/{decision.tp_option_id}"
                    fallback = f"combo_fallback: {chosen} -> {combo.label}"
                    logger.warning("%s %s (trader choice failed %s)", candidate.id, fallback, failed)
                    (entry, sl, tp), entry_type, verdict = combo.prices, combo.entry_type, v
                    break
            else:
                return EscalationResult(outcome="risk_rejected", scout_json=sj,
                                        trade_json=tj, detail=f"final_ladder: {failed}")

        plan = build_plan(candidate, verdict, scout_verdict, entry, sl, tp, entry_type,
                          fallback=fallback)
        expires = (datetime.now(UTC) + timedelta(seconds=self._ttl)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        j = self._state.journal
        j.execute(
            "INSERT OR IGNORE INTO pending_approvals (decision_id, card_json, expires_at) "
            "VALUES (?,?,?)",
            (candidate.id, plan.model_dump_json(), expires),
        )
        j.commit()
        # Режим «автомат» (Иван 2026-10-16): тот же план и те же проверки, что у кнопки, но без
        # нажатия. Не исполнилось (проверки/биржа) — карточка уходит с кнопками, решение за Иваном.
        auto = None
        if self._auto is not None and self._auto.mode.is_auto():
            auto = self._auto.run(candidate.id, entry_type)
        # 2026-10-16: подмена выбора Trader'а видна и в карточке, и в журнале (detail)
        note = "; ".join(n for n in (fallback, auto.note if auto is not None else "") if n)
        if self._card_sink is not None:
            try:
                if auto is None and not fallback:
                    message_id = self._card_sink(candidate, verdict, scout_verdict, plan)
                else:
                    message_id = self._card_sink(
                        candidate, verdict, scout_verdict, plan,
                        note=note, buttons=auto is None or not auto.executed,
                    )
                if message_id is False:  # Telegram отверг карточку: решение без карточки = TTL fail-closed
                    raise RuntimeError("telegram refused the card")
                if message_id:  # монитор снимет кнопки с карточки по истечении TTL
                    j.execute(
                        "UPDATE pending_approvals SET telegram_message_id=? WHERE decision_id=?",
                        (str(message_id), candidate.id),
                    )
                    j.commit()
            except Exception as e:  # noqa: BLE001 - TTL fail-closed прикроет
                logger.error("card sink failed for %s: %s", candidate.id, e)
        outcome = "executed" if auto is not None and auto.executed else "pending"
        return EscalationResult(outcome=outcome, scout_json=sj, trade_json=tj, detail=note,
                                prices=(entry, sl, tp))


def build_plan(
    candidate: SetupCandidate,
    verdict: RiskVerdict,
    scout_verdict: SetupVerdict,
    entry: float,
    sl: float,
    tp: float,
    entry_type: str,
    *,
    fallback: str = "",
) -> ExecutionPlan:
    """План исполнения: посчитанные цифры + лимитный уровень из меню.

    Лимитный уровень берем из выбора Trader'а, если он выбрал лимитку;
    иначе — лучшая лимитная опция меню (край FVG/OB), чтобы кнопка «лимит»
    была доступна даже когда Trader выбрал вход по рынку.

    Денежные поля — из вердикта как есть (ревью 2026-10-13, Codex #20/#21):
    leverage — целое, ровно то, что уйдет на биржу; risk_usd — потеря при
    стоп-ауте С ИЗДЕРЖКАМИ (ход до SL + проскальзывание stop-market + taker-
    комиссии входа и выхода) для qty при `entry`, <= caps.risk_per_trade_pct.
    Пересайзинг qty под другую цену входа — только sizing.qty_for_risk(risk_usd,
    вход, sl); деление на голую |вход - sl| снова выводит издержки за кап.

    fallback — пометка «combo_fallback: <выбор Trader'а> -> <взятая>» (2026-10-16):
    идет в tldr плана, а оттуда в trades.reasoning — подмена видна в сделке, контракт
    ExecutionPlan не меняется (в pending могут лежать планы старого формата).
    """
    limit_price = entry if entry_type == "limit" else None
    limit_note = ""
    if limit_price is None:
        for o in candidate.entry_options:
            if o.kind != "market":
                limit_price, limit_note = o.price, o.note
                break
    else:
        limit_note = next(
            (o.note for o in candidate.entry_options if o.price == entry and o.kind != "market"),
            "",
        )
    market_price = next(
        (o.price for o in candidate.entry_options if o.kind == "market"), entry
    )
    c = verdict.computed
    return ExecutionPlan(
        candidate_id=candidate.id,
        symbol=candidate.symbol,
        side=candidate.direction,
        entry_market=market_price,
        entry_limit=limit_price,
        entry_limit_note=limit_note,
        sl=sl,
        tp=tp,
        qty=c.qty,
        leverage=c.leverage,
        risk_usd=c.risk_usd,
        rr_fee_adjusted=c.rr_fee_adjusted,
        timeframe=candidate.timeframe,
        tldr=" | ".join(x for x in (fallback, getattr(scout_verdict, "tldr", "") or "") if x),
    )
