"""Реплей: детерминизм, префикс-дисциплина, разрешение исходов."""

import numpy as np
import pandas as pd
import pytest

from oskar.detect.smc import SmcConfig
from oskar.ops.replay import ReplayResult, replay, resolve_outcome
from oskar.risk.ladder import Caps, ProfileLimits

CAPS = Caps(
    risk_per_trade_pct=1.0, max_total_risk_pct=5.0, max_leverage=10,
    confidence_cap=75, min_rr_fee_adjusted=2.0, max_open_positions=3,
)
PROFILE = ProfileLimits(sl_dist_pct_min=0.3, sl_dist_pct_max=6.0, max_leverage=8)
SMC_CFG = SmcConfig(swing_length=5)


def market_df(n: int = 160, seed: int = 3) -> pd.DataFrame:
    """Волнистая серия с трендовыми ногами — порождает структуру."""
    rng = np.random.default_rng(seed)
    legs = np.concatenate([
        np.linspace(100, 112, 40),
        np.linspace(112, 104, 30),
        np.linspace(104, 118, 45),
        np.linspace(118, 108, 45),
    ])[:n]
    close = legs + rng.normal(0, 0.35, n)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    spread = np.abs(rng.normal(0, 0.3, n)) + 0.1
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    vol = np.abs(rng.normal(1000, 300, n))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol}
    )


# ------------------------------------------------------------ resolve_outcome


def outcome_df(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def test_resolve_long_tp():
    df = outcome_df([(100, 101, 99, 100, 1), (100, 106, 99.5, 105, 1)])
    assert resolve_outcome(df, 0, "long", sl=98.0, tp=104.0) == ("tp_hit", 1)


def test_resolve_short_sl():
    df = outcome_df([(100, 101, 99, 100, 1), (100, 103, 99.5, 102.5, 1)])
    assert resolve_outcome(df, 0, "short", sl=102.0, tp=95.0) == ("sl_hit", 1)


def test_resolve_same_bar_collision_sl_wins():
    """Бар зацепил и SL и TP — консервативно SL первым."""
    df = outcome_df([(100, 101, 99, 100, 1), (100, 105, 95, 100, 1)])
    assert resolve_outcome(df, 0, "long", sl=96.0, tp=104.0)[0] == "sl_hit"


def test_resolve_open_when_nothing_hit():
    df = outcome_df([(100, 101, 99, 100, 1), (100, 100.5, 99.5, 100, 1)])
    assert resolve_outcome(df, 0, "long", sl=95.0, tp=110.0) == ("open", None)


# --------------------------------------------------------------------- replay


@pytest.fixture(scope="module")
def run() -> ReplayResult:
    return replay(
        market_df(), "SOL/USDT:USDT", "15m", CAPS, PROFILE,
        min_score=30, smc_config=SMC_CFG,
    )


def test_replay_deterministic(run: ReplayResult):
    again = replay(
        market_df(), "SOL/USDT:USDT", "15m", CAPS, PROFILE,
        min_score=30, smc_config=SMC_CFG,
    )
    assert run.stats() == again.stats()
    assert [s.candidate.id for s in run.setups] == [s.candidate.id for s in again.setups]


def test_replay_produces_setups(run: ReplayResult):
    """Синтетика с ногами тренда обязана дать хотя бы один сетап."""
    assert run.setups, "no setups found — detectors or scoring broke"


def test_replay_prefix_discipline(run: ReplayResult):
    """Кандидат на баре t ссылается только на прошлое; исход — только на будущее."""
    for s in run.setups:
        assert s.candidate.id.endswith(f"-{s.bar_index}-{s.candidate.direction}")
        if s.resolved_index is not None:
            assert s.resolved_index > s.bar_index


def test_replay_risk_trace_present(run: ReplayResult):
    for s in run.setups:
        assert s.risk.steps, "risk verdict must carry full trace"
        if s.cohort == "executed":
            assert s.risk.passed and s.risk.computed is not None


def test_replay_stats_shape(run: ReplayResult):
    st = run.stats()
    assert st["setups_found"] == st["executed"] + st["risk_rejected"]
    assert st["resolved"] == st["tp_hits"] + st["sl_hits"]
