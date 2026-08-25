"""Реплей-харнесс: бар-за-баром через весь детерминированный конвейер.

Каждый бар t: analyze(candles[:t]) -> score -> options -> risk ladder.
НИКАКОГО расчета по всей серии: детекторы видят только префикс (защита от
look-ahead — см. detect/smc.py). Исход сетапа разрешается по последующим
барам: SL/TP по high/low; если оба в одном баре — консервативно SL первым.

Реплей БЕЗ LLM (экономика подписки): вместо Scout/Trader берутся дефолтные
опции меню (market-вход, свинг-SL, первый TP). Это оценка механического слоя
и генератор counterfactual-когорт, не бэктест стратегии Scout'а.
"""

from dataclasses import dataclass, field

import pandas as pd

from oskar.contracts.schemas import RiskVerdict, SetupCandidate
from oskar.detect.indicators import compute, tf_trend
from oskar.detect.options import build_candidate
from oskar.detect.score import score_opportunity
from oskar.detect.smc import DEFAULT_CONFIG, SmcConfig, analyze
from oskar.risk.ladder import Caps, ProfileLimits, RiskInput, evaluate

WARMUP_BARS = 60


@dataclass(frozen=True)
class ReplayedSetup:
    bar_index: int  # бар обнаружения (вход по close этого бара)
    candidate: SetupCandidate
    risk: RiskVerdict
    cohort: str  # executed | risk_rejected
    entry: float
    sl: float
    tp: float
    outcome: str  # tp_hit | sl_hit | open
    resolved_index: int | None


@dataclass
class ReplayResult:
    bars_processed: int = 0
    setups: list[ReplayedSetup] = field(default_factory=list)

    def cohort(self, name: str) -> list[ReplayedSetup]:
        return [s for s in self.setups if s.cohort == name]

    def stats(self) -> dict:
        executed = self.cohort("executed")
        resolved = [s for s in executed if s.outcome != "open"]
        wins = sum(1 for s in resolved if s.outcome == "tp_hit")
        return {
            "bars": self.bars_processed,
            "setups_found": len(self.setups),
            "executed": len(executed),
            "risk_rejected": len(self.cohort("risk_rejected")),
            "resolved": len(resolved),
            "tp_hits": wins,
            "sl_hits": len(resolved) - wins,
            "win_rate": round(wins / len(resolved), 3) if resolved else None,
        }


def resolve_outcome(
    df: pd.DataFrame, start: int, direction: str, sl: float, tp: float
) -> tuple[str, int | None]:
    """SL/TP по high/low последующих баров; оба в одном баре -> SL (консервативно)."""
    for j in range(start + 1, len(df)):
        hi, lo = float(df["high"].iloc[j]), float(df["low"].iloc[j])
        if direction == "long":
            sl_hit, tp_hit = lo <= sl, hi >= tp
        else:
            sl_hit, tp_hit = hi >= sl, lo <= tp
        if sl_hit:
            return "sl_hit", j  # SL первым при коллизии в одном баре
        if tp_hit:
            return "tp_hit", j
    return "open", None


def replay(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    caps: Caps,
    profile: ProfileLimits,
    equity_usd: float = 1000.0,
    min_score: int = 70,
    smc_config: SmcConfig = DEFAULT_CONFIG,
    cooldown_bars: int = 20,
    confidence: int = 68,
) -> ReplayResult:
    """Один символ, один ТФ. df — закрытые свечи с RangeIndex."""
    result = ReplayResult()
    last_signal_bar = -(10**9)

    for t in range(WARMUP_BARS, len(df) + 1):
        prefix = df.iloc[:t].reset_index(drop=True)
        result.bars_processed += 1
        if t - 1 - last_signal_bar < cooldown_bars:
            continue

        snapshot = analyze(prefix, smc_config)
        ind = compute(prefix)
        mtf = tf_trend(ind)  # одиночный ТФ: его тренд = mtf-приближение
        opp = score_opportunity(snapshot, ind, mtf)
        if opp is None or opp.score < min_score:
            continue

        candidate = build_candidate(symbol, timeframe, snapshot, ind, opp, mtf)
        if candidate is None:
            continue

        # дефолтные опции: market-вход, первый SL (свинг), первый TP
        entry = next(o for o in candidate.entry_options if o.kind == "market").price
        sl = candidate.sl_options[0].price
        tp = candidate.tp_options[0].price

        verdict = evaluate(
            RiskInput(
                candidate=candidate,
                entry=entry, sl=sl, tp=tp,
                confidence=confidence,
                equity_usd=equity_usd,
            ),
            caps, profile,
        )
        cohort = "executed" if verdict.passed else "risk_rejected"
        outcome, resolved = resolve_outcome(df, t - 1, candidate.direction, sl, tp)
        result.setups.append(
            ReplayedSetup(
                bar_index=t - 1, candidate=candidate, risk=verdict, cohort=cohort,
                entry=entry, sl=sl, tp=tp, outcome=outcome, resolved_index=resolved,
            )
        )
        last_signal_bar = t - 1

    return result
