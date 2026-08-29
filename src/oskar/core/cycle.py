"""Детерминированный цикл демона — Фаза 1: SHADOW-режим без LLM и ордеров.

Каждый проход: свечи -> SMC-анализ -> скоринг -> меню опций -> риск-лестница
(с дефолтной confidence) -> журнал decisions + counterfactuals (cohort=shadow).
Эскалация к Scout/Trader и исполнение подключаются в Фазе 2 — цикл уже сейчас
накапливает counterfactual-датасет: «что бы система выбрала и чем бы это
кончилось» (исходы дорешивает поздний проход по свечам).

Look-ahead дисциплина: cache отдает только закрытые свечи (client отбросил
формирующуюся), analyze видит только префикс.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from oskar.core.state import RuntimeState
from oskar.data.candles import CandleCache
from oskar.detect.indicators import IndicatorSet, compute, multi_tf_trend
from oskar.detect.options import build_candidate
from oskar.detect.score import score_opportunity
from oskar.detect.smc import DEFAULT_CONFIG, SmcConfig, analyze
from oskar.risk.ladder import Caps, ProfileLimits, RiskInput, evaluate

SHADOW_CONFIDENCE = 68  # фиксированная confidence shadow-режима (в границах [60..75])


def _iso_to_ms(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp() * 1000.0


@dataclass
class CycleConfig:
    signal_timeframe: str = "15m"
    context_timeframes: tuple[str, ...] = ("1h", "4h")
    candles_limit: int = 300
    min_score: int = 70
    cooldown_bars: int = 30
    approval_ttl_seconds: int = 1200  # profiles.yaml: intraday 20 мин
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
    ):
        self._cache = cache
        self._state = state
        self._caps = caps
        self._profile = profile
        self._cfg = config or CycleConfig()
        self._card_sink = card_sink
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

        if symbol in self._last_bar:
            if snapshot.bar_index - self._last_bar[symbol] < cfg.cooldown_bars:
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

        candidate = build_candidate(symbol, cfg.signal_timeframe, snapshot, ind, opp, mtf)
        if candidate is None:
            return
        report.candidates += 1
        self._last_bar[symbol] = snapshot.bar_index

        entry = next(o for o in candidate.entry_options if o.kind == "market").price
        sl = candidate.sl_options[0].price
        tp = candidate.tp_options[0].price
        verdict = evaluate(
            RiskInput(
                candidate=candidate, entry=entry, sl=sl, tp=tp,
                confidence=SHADOW_CONFIDENCE, equity_usd=equity_usd,
            ),
            self._caps, self._profile,
        )
        if verdict.passed:
            report.risk_passed += 1

        j = self._state.journal
        j.execute(
            "INSERT OR IGNORE INTO decisions (run_id, decision_id, candidate_json, "
            "risk_json, outcome, outcome_detail) VALUES (?,?,?,?,?,?)",
            (
                self._state.run_id, candidate.id, candidate.model_dump_json(),
                verdict.model_dump_json(),
                "risk_rejected" if not verdict.passed else "scout_pass",
                "shadow_mode" if self._card_sink is None else "live_pending",
            ),
        )
        j.execute(
            "INSERT OR IGNORE INTO counterfactuals (decision_id, cohort, candidate_json, "
            "entry_price, sl_price, tp_price) VALUES (?,?,?,?,?,?)",
            (
                candidate.id,
                "shadow" if verdict.passed else "risk_rejected",
                candidate.model_dump_json(), entry, sl, tp,
            ),
        )
        if verdict.passed and self._card_sink is not None:
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

    def resolve_counterfactuals(self, symbol: str) -> int:
        """Дорешивает висящие counterfactuals символа по свежим закрытым свечам."""
        j = self._state.journal
        rows = j.execute(
            "SELECT id, decision_id, candidate_json, entry_price, sl_price, tp_price "
            "FROM counterfactuals WHERE hypothetical_outcome IS NULL"
        ).fetchall()
        if not rows:
            return 0
        df = self._cache.get(symbol, self._cfg.signal_timeframe, self._cfg.candles_limit)
        resolved = 0
        for r in rows:
            cand = json.loads(r["candidate_json"])
            if cand["symbol"] != symbol:
                continue
            direction = cand["direction"]
            # только бары, ОТКРЫВШИЕСЯ ПОСЛЕ создания кандидата (анти-look-ahead)
            created_ms = _iso_to_ms(cand["ts"])
            future = df[df["ts"] > created_ms]
            outcome = None
            for i in range(len(future)):
                hi, lo = float(future["high"].iloc[i]), float(future["low"].iloc[i])
                if direction == "long":
                    sl_hit, tp_hit = lo <= r["sl_price"], hi >= r["tp_price"]
                else:
                    sl_hit, tp_hit = hi >= r["sl_price"], lo <= r["tp_price"]
                if sl_hit:
                    outcome = "sl_hit"
                    break
                if tp_hit:
                    outcome = "tp_hit"
                    break
            if outcome is not None:
                j.execute(
                    "UPDATE counterfactuals SET hypothetical_outcome=?, "
                    "resolved_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                    (outcome, r["id"]),
                )
                resolved += 1
        j.commit()
        return resolved
