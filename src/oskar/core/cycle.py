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
from oskar.detect.candles import shadow_or_none
from oskar.detect.indicators import IndicatorSet, compute, multi_tf_trend
from oskar.detect.options import build_candidate
from oskar.detect.score import score_opportunity
from oskar.detect.smc import DEFAULT_CONFIG, SmcConfig, analyze
from oskar.risk.combos import choose_combo, evaluate_combos, most_advanced
from oskar.risk.ladder import Caps, ProfileLimits

SHADOW_CONFIDENCE = 68  # фиксированная confidence shadow-режима (в границах [60..75])
EXPIRED_FLAT_BARS = 48  # counterfactual без исхода за 48 баров (12ч на 15м) -> expired_flat


def _iso_to_ms(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp() * 1000.0


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
        frames = {cfg.signal_timeframe: df}  # свечи для тени паттернов (блок J)
        for tf in cfg.context_timeframes:
            frames[tf] = self._cache.get(symbol, tf, 260)
            per_tf[tf] = compute(frames[tf])
        mtf = multi_tf_trend(per_tf)
        ind = per_tf[cfg.signal_timeframe]

        opp = score_opportunity(snapshot, ind, mtf)
        if opp is None:
            return
        report.scored += 1
        if opp.score < cfg.min_score:
            return

        # Блок J (2026-10-16): тень свечных паттернов по последним закрытым свечам уже
        # загруженных ТФ (новых запросов нет). Только журнал: candle_pattern пуст, гейт и
        # промпты ее не видят; сбой детектора -> None, символ из цикла не выпадает.
        candidate = build_candidate(
            symbol, cfg.signal_timeframe, snapshot, ind, opp, mtf, bar_ts=bar_ts,
            candle_shadow=shadow_or_none(frames),
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
        j.execute(
            "INSERT OR IGNORE INTO counterfactuals (decision_id, cohort, candidate_json, "
            "entry_price, sl_price, tp_price) VALUES (?,?,?,?,?,?)",
            (candidate.id, cohort, candidate.model_dump_json(), entry, sl, tp),
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
        (символ выпал из топа -> исход не записывался никогда; 32/82 висели с августа)."""
        rows = self._state.journal.execute(
            "SELECT DISTINCT json_extract(candidate_json, '$.symbol') AS symbol "
            "FROM counterfactuals WHERE hypothetical_outcome IS NULL"
        ).fetchall()
        return sorted(r["symbol"] for r in rows if r["symbol"])

    def resolve_counterfactuals(self, symbol: str) -> int:
        """Дорешивает висящие counterfactuals символа по свежим закрытым свечам.

        Ни SL, ни TP за EXPIRED_FLAT_BARS баров -> expired_flat (сетап протух)."""
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
        df = self._cache.get(symbol, self._cfg.signal_timeframe, self._cfg.candles_limit)
        resolved = 0
        for r in rows:
            cand = json.loads(r["candidate_json"])
            direction = cand["direction"]
            # только бары, ОТКРЫВШИЕСЯ ПОСЛЕ создания кандидата (анти-look-ahead) и
            # в пределах горизонта: старые уровни против сегодняшних цен — не исход
            created_ms = _iso_to_ms(cand["ts"])
            bar_ms = int(df["ts"].iloc[-1] - df["ts"].iloc[-2]) if len(df) >= 2 else 900_000
            horizon_ms = created_ms + EXPIRED_FLAT_BARS * bar_ms
            future = df[(df["ts"] > created_ms) & (df["ts"] <= horizon_ms)]
            # лимитный вход сначала должен быть ДОСТИГНУТ: иначе TP засчитывался
            # сетапу, в который никто бы не вошел (Codex 2026-10-13: limit 90, бар 98-106 -> tp_hit)
            market_px = next(
                (o["price"] for o in cand.get("entry_options", []) if o.get("kind") == "market"), None
            )
            entered = market_px is None or abs(float(r["entry_price"]) - float(market_px)) < 1e-12
            outcome = None
            for i in range(len(future)):
                hi, lo = float(future["high"].iloc[i]), float(future["low"].iloc[i])
                if not entered:
                    touched = lo <= r["entry_price"] if direction == "long" else hi >= r["entry_price"]
                    if not touched:
                        continue
                    entered = True
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
            if outcome is None and int(df["ts"].iloc[-1]) >= horizon_ms:
                outcome = "expired_flat"  # горизонт прошел без касания SL/TP (или без данных)
            if outcome is not None:
                j.execute(
                    "UPDATE counterfactuals SET hypothetical_outcome=?, "
                    "resolved_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                    (outcome, r["id"]),
                )
                resolved += 1
        j.commit()
        return resolved
