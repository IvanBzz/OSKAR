"""Детерминированный цикл демона — Фаза 1: SHADOW-режим без LLM и ордеров.

Каждый проход: свечи -> SMC-анализ -> скоринг -> меню опций -> риск-лестница
(с дефолтной confidence) -> журнал decisions + counterfactuals (cohort=shadow).
Эскалация к Scout/Trader и исполнение подключаются в Фазе 2 — цикл уже сейчас
накапливает counterfactual-датасет: «что бы система выбрала и чем бы это
кончилось» (исходы дорешивает поздний проход по свечам — core/cf_resolver.py, v2).

Look-ahead дисциплина: cache отдает только закрытые свечи (client отбросил
формирующуюся), analyze видит только префикс.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from oskar.core.cf_resolver import CF_RESOLVER_VERSION, ResolverParams, resolve
from oskar.core.state import RuntimeState
from oskar.data.candles import CandleCache
from oskar.detect.indicators import IndicatorSet, compute, multi_tf_trend
from oskar.detect.options import build_candidate
from oskar.detect.score import score_opportunity
from oskar.detect.smc import DEFAULT_CONFIG, SmcConfig, analyze
from oskar.journal.db import transaction
from oskar.journal.episodes import TIMEFRAME_MS, assign_episode
from oskar.risk.combos import choose_combo, evaluate_combos, most_advanced
from oskar.risk.ladder import Caps, ProfileLimits

SHADOW_CONFIDENCE = 68  # фиксированная confidence shadow-режима (в границах [60..75])


def best_passing_combo(candidate, caps, profile, *, confidence, equity_usd,
                       safe_mode=False, paused=False):
    """Перебор всех комбинаций меню через лестницу; проходная по правилу choose_combo.

    Гейт эскалации по ОДНОЙ дефолтной комбинации несправедливо строг (живой
    датасет: 12/17 кандидатов срезаны по R:R на market-входе с первым TP, при
    том что лимитка от FVG и тейк 2R прошли бы). Trader существует для выбора
    комбинации — гейт обязан проверять достижимость, а не один вариант.

    Возвращает (verdict, entry, sl, tp) комбинации, которую берет единое правило
    (risk/combos.rank_combos: минимальный R:R над капом — ближайшая реалистичная
    цель; 2026-10-16 вместо максимального R:R, мерившего counterfactual по дальним
    тейкам). Если не проходит ни одна — вердикт комбинации, прошедшей больше всего
    шагов лестницы (при равенстве первой). 2026-10-16: раньше бралась дефолтная
    (первая), лестница на ней обрывалась на R:R, и журнал писал «R:R», хотя почти
    всё резал confluence_gate шагом дальше — диагностика врала.
    """
    evals = evaluate_combos(
        candidate, caps, profile, confidence=confidence, equity_usd=equity_usd,
        safe_mode=safe_mode, paused=paused,
    )
    pick = choose_combo(evals) or most_advanced(evals)
    return (pick.verdict, *pick.prices)


@dataclass
class CycleConfig:
    signal_timeframe: str = "15m"
    context_timeframes: tuple[str, ...] = ("1h", "4h")
    candles_limit: int = 300
    min_score: int = 70
    cooldown_bars: int = 30
    approval_ttl_seconds: int = 1200  # profiles.yaml: intraday 20 мин
    # 2026-10-16 (блок R3): жизненный цикл живой системы для резолвера CF v2 (profiles.yaml)
    limit_entry_ttl_seconds: int = 3600
    max_loss_hold_seconds: int = 10800
    smc: SmcConfig = DEFAULT_CONFIG


@dataclass
class CycleReport:
    scanned: int = 0
    scored: int = 0
    candidates: int = 0
    risk_passed: int = 0
    errors: list[str] = field(default_factory=list)


class ShadowCycle:
    """card_sink=None -> чистый shadow. С card_sink (Фаза 2) прошедшие лестницу
    кандидаты дополнительно попадают в pending_approvals (TTL fail-closed) и
    уходят карточкой в Telegram. Исполнение по approve — отдельный компонент."""

    def __init__(
        self,
        cache: CandleCache,
        state: RuntimeState,
        caps: Caps,
        profile: ProfileLimits,
        config: CycleConfig | None = None,
        card_sink=None,  # Callable[[SetupCandidate, RiskVerdict], None] | None
        escalator=None,  # Callable[[SetupCandidate], EscalationResult] | None (Фаза 2)
    ):
        self._cache = cache
        self._state = state
        self._caps = caps
        self._profile = profile
        self._cfg = config or CycleConfig()
        self._card_sink = card_sink
        self._escalator = escalator
        self._last_bar: dict[str, int] = {}  # symbol -> bar_index последнего кандидата

    def run_once(self, symbols: list[str], equity_usd: float) -> CycleReport:
        report = CycleReport()
        if self._state.paused or self._state.safe_mode:
            return report
        for symbol in symbols:
            try:
                self._process(symbol, equity_usd, report)
            except Exception as e:  # noqa: BLE001 - один символ не валит цикл
                report.errors.append(f"{symbol}: {type(e).__name__}: {e}")
        return report

    def _process(self, symbol: str, equity_usd: float, report: CycleReport) -> None:
        cfg = self._cfg
        report.scanned += 1

        df = self._cache.get(symbol, cfg.signal_timeframe, cfg.candles_limit)
        snapshot = analyze(df, cfg.smc)

        # Ключ бара — таймстамп открытия: bar_index скользящего окна всегда 299
        # (кулдаун «299-299<2» не истекал никогда, id решений совпадали)
        bar_ts = int(df["ts"].iloc[-1])
        bar_ms = int(df["ts"].iloc[-1] - df["ts"].iloc[-2]) if len(df) >= 2 else 0
        if symbol in self._last_bar:
            if bar_ts - self._last_bar[symbol] < cfg.cooldown_bars * bar_ms:
                return

        per_tf: dict[str, IndicatorSet] = {cfg.signal_timeframe: compute(df)}
        for tf in cfg.context_timeframes:
            per_tf[tf] = compute(self._cache.get(symbol, tf, 260))
        mtf = multi_tf_trend(per_tf)
        ind = per_tf[cfg.signal_timeframe]

        opp = score_opportunity(snapshot, ind, mtf)
        if opp is None:
            return
        report.scored += 1
        if opp.score < cfg.min_score:
            return

        candidate = build_candidate(
            symbol, cfg.signal_timeframe, snapshot, ind, opp, mtf, bar_ts=bar_ts
        )
        if candidate is None:
            return
        report.candidates += 1
        self._last_bar[symbol] = bar_ts

        verdict, entry, sl, tp = best_passing_combo(
            candidate, self._caps, self._profile,
            confidence=SHADOW_CONFIDENCE, equity_usd=equity_usd,
            safe_mode=self._state.safe_mode, paused=self._state.paused,
        )
        if verdict.passed:
            report.risk_passed += 1

        outcome = "risk_rejected" if not verdict.passed else "scout_pass"
        detail = "shadow_mode" if self._card_sink is None else "live_pending"
        cohort = "risk_rejected" if not verdict.passed else "shadow"
        scout_json = trade_json = None

        if verdict.passed and self._escalator is not None:
            esc = self._escalator(candidate)
            outcome = esc.outcome if esc.outcome != "error" else "error"
            detail = esc.detail or "escalated"
            scout_json, trade_json = esc.scout_json, esc.trade_json
            # 2026-10-16 (журнал v4): эскалированные — в свои когорты, а не в 'shadow'
            # вперемешку с чистым shadow (CHECK раньше не пускал executed/pending/error)
            cohort = {
                "scout_pass": "scout_pass",
                "trader_decline": "trader_decline",
                "risk_rejected": "risk_rejected",
                "pending": "pending",
                "executed": "executed",  # автомат: counterfactual рядом с реальной сделкой
                "error": "error",
            }[esc.outcome]
            # 2026-10-16: цены ФАКТИЧЕСКИ выбранной комбинации (с учетом combo_fallback),
            # а не лучшей по R:R из гейта — иначе исход считался не по той сделке
            if esc.prices is not None:
                entry, sl, tp = esc.prices

        j = self._state.journal
        j.execute(
            "INSERT OR IGNORE INTO decisions (run_id, decision_id, candidate_json, "
            "scout_json, trade_json, risk_json, outcome, outcome_detail) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                self._state.run_id, candidate.id, candidate.model_dump_json(),
                scout_json, trade_json, verdict.model_dump_json(), outcome, detail,
            ),
        )
        # v5 (блок R3): версия резолвера, которым будет посчитан исход, и серия сетапа
        episode = assign_episode(
            j, candidate.symbol, candidate.direction,
            int(candidate.ts.timestamp() * 1000), candidate.timeframe,
        )
        j.execute(
            "INSERT OR IGNORE INTO counterfactuals (decision_id, cohort, candidate_json, "
            "entry_price, sl_price, tp_price, resolver_version, episode_id) VALUES (?,?,?,?,?,?,?,?)",
            (candidate.id, cohort, candidate.model_dump_json(), entry, sl, tp,
             CF_RESOLVER_VERSION, episode),
        )
        if verdict.passed and self._card_sink is not None and self._escalator is None:
            expires = (
                datetime.now(UTC) + timedelta(seconds=self._cfg.approval_ttl_seconds)
            ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            j.execute(
                "INSERT OR IGNORE INTO pending_approvals (decision_id, card_json, expires_at) "
                "VALUES (?,?,?)",
                (candidate.id, candidate.model_dump_json(), expires),
            )
            j.commit()
            try:
                self._card_sink(candidate, verdict)
            except Exception as e:  # noqa: BLE001 - канал упал: TTL все равно fail-closed
                report.errors.append(f"card_sink {candidate.id}: {type(e).__name__}: {e}")
            return
        j.commit()

    def unresolved_symbols(self) -> list[str]:
        """Символы с висящими counterfactuals — дорешивать надо их, а не вотчлист
        (символ выпал из топа -> исход не записывался никогда; 32/82 висели с августа).

        Порядок — от самой старой висящей строки (2026-10-16, R3): runner берет первые
        MAX_RESOLVE_SYMBOLS, и при алфавитном порядке хвост алфавита мог ждать, пока окно не
        уйдет из кеша свечей — у резолвера v2 это no_data навсегда."""
        rows = self._state.journal.execute(
            "SELECT json_extract(candidate_json, '$.symbol') AS symbol, MIN(id) AS first_id "
            "FROM counterfactuals WHERE hypothetical_outcome IS NULL "
            "GROUP BY symbol ORDER BY first_id, symbol"
        ).fetchall()
        return [r["symbol"] for r in rows if r["symbol"]]

    def resolve_counterfactuals(self, symbol: str) -> int:
        """Дорешивает висящие counterfactuals символа резолвером v2 (core/cf_resolver.py).

        Свечи — тот же вызов кеша, что у цикла (без новых запросов к бирже): последние
        candles_limit закрытых баров. Окно не покрыто — строка ждет; окно старше кеша или
        дыра в свечах — no_data. Исход пишется с resolver_version=2, R после комиссий и
        деталями (покрытие, вход, выход, ambiguous) в outcome_detail."""
        j = self._state.journal
        # Ревью 2026-10-13, E.4: отбор в SQL не загружает весь хвост на каждый символ.
        rows = j.execute(
            "SELECT id, decision_id, candidate_json, entry_price, sl_price, tp_price "
            "FROM counterfactuals WHERE hypothetical_outcome IS NULL "
            "AND json_extract(candidate_json, '$.symbol') = ?",
            (symbol,),
        ).fetchall()
        if not rows:
            return 0
        params = ResolverParams(
            limit_entry_ttl_seconds=self._cfg.limit_entry_ttl_seconds,
            max_loss_hold_seconds=self._cfg.max_loss_hold_seconds,
        )
        frames: dict[str, object] = {}
        results = []
        for r in rows:
            try:
                cand = json.loads(r["candidate_json"])
            except (TypeError, ValueError):
                cand = None
            cand = cand if isinstance(cand, dict) else {}
            if not cand.get("timeframe"):  # ТФ свечей и ТФ резолвера — один и тот же
                cand["timeframe"] = self._cfg.signal_timeframe
            tf = cand["timeframe"]
            if tf in TIMEFRAME_MS and tf not in frames:  # обычно один ТФ — один вызов кеша, как в v1
                frames[tf] = self._cache.get(symbol, tf, self._cfg.candles_limit)
            # неизвестный ТФ — без запроса к бирже: resolve() вернет no_data (bad_candidate)
            res = resolve(cand, r["entry_price"], r["sl_price"], r["tp_price"], frames.get(tf), params)
            if res is not None:
                results.append((res.outcome, res.detail_json(), res.realized_r, CF_RESOLVER_VERSION,
                                r["id"]))
        if results:
            with transaction(j):
                j.executemany(
                    "UPDATE counterfactuals SET hypothetical_outcome=?, outcome_detail=?, "
                    "realized_r=?, resolver_version=?, "
                    "resolved_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE id=? AND hypothetical_outcome IS NULL",
                    results,
                )
        return len(results)
