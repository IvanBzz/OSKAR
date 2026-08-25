"""Индикаторы и скоринг: тренды, компоненты, направление из структуры."""

import numpy as np
import pandas as pd
import pytest

from oskar.detect.indicators import (
    IndicatorSet,
    compute,
    multi_tf_trend,
    tf_trend,
)
from oskar.detect.score import Opportunity, score_opportunity
from oskar.detect.smc import (
    FvgZone,
    OrderBlock,
    SmcSnapshot,
    StructureEvent,
)


def trending_df(n: int = 250, up: bool = True) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    slope = 0.3 if up else -0.3
    close = 100 + slope * np.arange(n) + rng.normal(0, 0.3, n)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + 0.2
    low = np.minimum(open_, close) - 0.2
    vol = np.full(n, 1000.0)
    vol[-1] = 2000.0  # всплеск на последней свече
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol}
    )


def ind_stub(**overrides) -> IndicatorSet:
    base = dict(
        rsi=50.0, ema20=100.0, ema50=99.0, ema200=None, atr=1.0, atr_pct=1.0,
        macd_hist=0.1, macd_cross="none", volume_ratio=1.0, close=100.0,
    )
    base.update(overrides)
    return IndicatorSet(**base)


def snapshot_stub(direction: str = "bearish", bar_index: int = 100) -> SmcSnapshot:
    return SmcSnapshot(
        bar_index=bar_index,
        detector_version="test",
        structure=[
            StructureEvent(
                kind="choch", direction=direction, swing_index=80,
                level=101.0, broken_index=bar_index - 5,
            )
        ],
        fvgs=[
            FvgZone(index=90, direction=direction, top=101.0, bottom=100.2, mitigated=False)
        ],
        order_blocks=[
            OrderBlock(
                index=85, direction=direction, top=101.5, bottom=100.8,
                volume=1.0, strength_pct=50.0, mitigated=False,
            )
        ],
    )


# ----------------------------------------------------------------- indicators


def test_compute_uptrend():
    ind = compute(trending_df(up=True))
    assert tf_trend(ind) == "bullish"
    assert ind.rsi > 50
    assert ind.volume_ratio > 1.5
    assert ind.ema200 is not None  # 250 свечей достаточно


def test_compute_downtrend():
    ind = compute(trending_df(up=False))
    assert tf_trend(ind) == "bearish"


def test_compute_short_history_raises():
    with pytest.raises(ValueError):
        compute(trending_df(n=30))


def test_multi_tf_trend_weighted():
    bull = ind_stub(close=101.0, ema20=100.0, ema50=99.0)
    bear = ind_stub(close=98.0, ema20=99.0, ema50=100.0)
    # старшие ТФ (4h=3.5, 1d=5.0) быки против младших (15m=1, 1h=2) медведей
    assert multi_tf_trend({"15m": bear, "1h": bear, "4h": bull, "1d": bull}) == "bullish"
    # поровну по весу -> mixed
    assert multi_tf_trend({"15m": bull, "1h": bull, "4h": bear}) == "mixed"


# ---------------------------------------------------------------------- score


def test_direction_follows_structure_not_indicators():
    """Медвежий CHoCH -> short, даже при бычьих индикаторах."""
    opp = score_opportunity(
        snapshot_stub("bearish"),
        ind_stub(rsi=65.0),
        mtf_trend="bullish",  # тренд против структуры -> 0 очков за тренд
    )
    assert opp is not None
    assert opp.direction == "short"
    assert opp.components["trend"] == 0


def test_no_structure_returns_none():
    empty = SmcSnapshot(bar_index=100, detector_version="test")
    assert score_opportunity(empty, ind_stub(), "bullish") is None


def test_stale_structure_returns_none():
    old = snapshot_stub("bearish", bar_index=200)
    # событие на 195 при recency 40 живо; сдвинем bar_index так, чтобы протухло
    stale = SmcSnapshot(
        bar_index=300, detector_version="test", structure=old.structure
    )
    assert score_opportunity(stale, ind_stub(), "bearish") is None


def test_full_confluence_scores_high():
    """Все компоненты в сборе: score >= 70 (порог эскалации)."""
    opp = score_opportunity(
        snapshot_stub("bearish"),
        ind_stub(rsi=55.0, volume_ratio=2.0, macd_cross="bear_cross", close=100.9),
        mtf_trend="bearish",
    )
    assert opp is not None and opp.direction == "short"
    assert opp.components == {
        "fvg": 15, "ob": 15, "structure": 10, "trend": 25,
        "volume": 15, "rsi": 10, "macd": 10,
    }
    assert opp.score == 100


def test_weak_setup_scores_below_threshold():
    """Структура есть, но далеко от цены, без тренда/объема -> < 70."""
    far = SmcSnapshot(
        bar_index=100,
        detector_version="test",
        structure=[
            StructureEvent(
                kind="bos", direction="bearish", swing_index=50,
                level=120.0, broken_index=70,
            )
        ],
        fvgs=[FvgZone(index=60, direction="bearish", top=120.0, bottom=118.0, mitigated=False)],
    )
    opp = score_opportunity(far, ind_stub(close=100.0), "mixed")
    assert opp is not None
    assert opp.score < 70


def test_score_is_opportunity_dataclass():
    opp = score_opportunity(snapshot_stub(), ind_stub(), "bearish")
    assert isinstance(opp, Opportunity)
    assert 0 <= opp.score <= 100
