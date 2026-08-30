"""Эскалация кандидата к LLM: Scout -> Trader(вето) -> лестница на выбранных
опциях -> pending_approvals + карточка в Telegram.

Порядок жесткий (ст. 2): даже после «execute» Trader'а выбранная комбинация
цен проходит ПОЛНУЮ риск-лестницу заново — LLM не источник чисел, а селектор.
Каждый LLM-вызов журналируется внутри агентов (analysis_log).

scout_fn/trader_fn инжектируются (тесты — стабы, прод — реальные агенты).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from oskar.agents.budget import BudgetExceeded
from oskar.contracts.schemas import SetupCandidate, SetupVerdict, TradeDecision
from oskar.core.state import RuntimeState
from oskar.risk.ladder import Caps, ProfileLimits, RiskInput, evaluate

logger = logging.getLogger("oskar.escalation")


@dataclass(frozen=True)
class EscalationResult:
    outcome: str  # scout_pass | trader_decline | risk_rejected | pending | error
    scout_json: str | None = None
    trade_json: str | None = None
    detail: str = ""


class Escalator:
    def __init__(
        self,
        state: RuntimeState,
        caps: Caps,
        profile: ProfileLimits,
        *,
        scout_fn,   # (candidate) -> SetupVerdict
        trader_fn,  # (candidate, scout_verdict) -> TradeDecision
        resolve_fn,  # (candidate, decision) -> (entry, sl, tp, entry_type)
        approval_ttl_seconds: int,
        card_sink=None,  # (candidate, verdict, scout_verdict) -> None
        open_positions_fn=lambda: [],
        equity_fn=lambda: 1000.0,
    ):
        self._state = state
        self._caps = caps
        self._profile = profile
        self._scout = scout_fn
        self._trader = trader_fn
        self._resolve = resolve_fn
        self._ttl = approval_ttl_seconds
        self._card_sink = card_sink
        self._open_positions = open_positions_fn
        self._equity = equity_fn

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

        try:
            decision: TradeDecision = self._trader(candidate, scout_verdict)
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

        entry, sl, tp, entry_type = self._resolve(candidate, decision)
        verdict = evaluate(
            RiskInput(
                candidate=candidate, entry=entry, sl=sl, tp=tp,
                confidence=scout_verdict.confidence,
                equity_usd=float(self._equity()),
                open_positions=list(self._open_positions()),
                safe_mode=self._state.safe_mode,
                paused=self._state.paused,
            ),
            self._caps, self._profile,
        )
        if not verdict.passed:
            failed = next((s.name for s in verdict.steps if not s.passed), "?")
            return EscalationResult(outcome="risk_rejected", scout_json=sj,
                                    trade_json=tj, detail=f"final_ladder: {failed}")

        expires = (datetime.now(UTC) + timedelta(seconds=self._ttl)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        j = self._state.journal
        j.execute(
            "INSERT OR IGNORE INTO pending_approvals (decision_id, card_json, expires_at) "
            "VALUES (?,?,?)",
            (candidate.id, candidate.model_dump_json(), expires),
        )
        j.commit()
        if self._card_sink is not None:
            try:
                self._card_sink(candidate, verdict, scout_verdict)
            except Exception as e:  # noqa: BLE001 - TTL fail-closed прикроет
                logger.error("card sink failed for %s: %s", candidate.id, e)
        return EscalationResult(outcome="pending", scout_json=sj, trade_json=tj)
