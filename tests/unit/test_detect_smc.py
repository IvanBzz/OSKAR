"""Детект-слой: look-ahead маски, префикс-детерминизм, ручные FVG-кейсы.

Это тесты против «самого вероятного тихого бага» (план): фантазийные
paper-результаты из-за структур, известных только из будущего.
"""

import numpy as np
import pandas as pd
import pytest

from oskar.detect.smc import (
    SmcConfig,
    analyze,
    confirmed_swings,
)

CFG = SmcConfig(swing_length=10)


def synthetic_ohlcv(n: int = 600, seed: int = 42) -> pd.DataFrame:
    """Детерминированная серия: синус-тренд + шум, реалистичные OHLC."""
    rng = np.random.default_rng(seed)
    drift = np.sin(np.linspace(0, 6 * np.pi, n)) * 8
    noise = rng.normal(0, 1, n).cumsum()
    close = 100 + drift + noise
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    spread = np.abs(rng.normal(0, 0.6, n)) + 0.2
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = np.abs(rng.normal(1000, 200, n))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )


def fvg_handcrafted() -> pd.DataFrame:
    """Бычий FVG: high[0]=10 < low[2]=12, свеча 1 бычья. Свеча 5 митигирует."""
    rows = [
        # open, high, low, close, volume
        (9.5, 10.0, 9.0, 9.8, 100),    # 0
        (9.8, 12.5, 9.7, 12.4, 500),   # 1 импульс вверх (бычья)
        (12.4, 13.0, 12.0, 12.8, 300), # 2 low=12 > high[0]=10 -> FVG(1): top=12, bottom=10
        (12.8, 13.2, 12.5, 13.0, 200), # 3
        (13.0, 13.1, 12.6, 12.7, 150), # 4
        (12.7, 12.8, 11.5, 11.8, 400), # 5 low=11.5 <= top=12 -> митигация
        (11.8, 12.1, 11.6, 12.0, 100), # 6
    ]
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


# ------------------------------------------------------------- look-ahead маски


def test_no_swings_in_unconfirmed_tail():
    df = synthetic_ohlcv()
    snap = analyze(df, CFG)
    cutoff = len(df) - CFG.swing_length
    assert snap.swings, "synthetic data must produce swings"
    assert all(s.index < cutoff for s in snap.swings)


def test_forced_endpoint_swings_are_masked():
    """Библиотека принудительно метит бар 0 и последний бар — оба маскируются."""
    df = synthetic_ohlcv()
    masked = confirmed_swings(df, CFG.swing_length)
    assert np.isnan(masked.loc[0, "HighLow"])
    assert np.isnan(masked.loc[len(df) - 1, "HighLow"])
    # а сырая библиотека их действительно метит (иначе маска бессмысленна)
    from smartmoneyconcepts import smc as raw_smc

    raw = raw_smc.swing_highs_lows(df, swing_length=CFG.swing_length)
    assert not np.isnan(raw.loc[len(df) - 1, "HighLow"])


def test_structure_events_reference_confirmed_swings_only():
    df = synthetic_ohlcv()
    snap = analyze(df, CFG)
    cutoff = len(df) - CFG.swing_length
    for ev in snap.structure:
        assert ev.swing_index < cutoff
        assert ev.broken_index <= snap.bar_index
        assert ev.broken_index > ev.swing_index  # событие узнается ПОЗЖЕ разметки


def test_last_bar_never_carries_fvg():
    """FVG требует shift(-1): на префиксе последний бар не может нести метку."""
    df = synthetic_ohlcv()
    snap = analyze(df, CFG)
    assert all(z.index < snap.bar_index for z in snap.fvgs)


# --------------------------------------------------------- префикс-детерминизм


def test_analyze_deterministic():
    df = synthetic_ohlcv()
    a, b = analyze(df, CFG), analyze(df, CFG)
    assert a == b


def test_replay_loop_prefix_safety():
    """Реплей-паттерн: analyze(candles[:t]) на каждом баре. Все репортуемые
    структуры ссылаются только на прошлое; крешей и утечек индексов нет."""
    df = synthetic_ohlcv(300)
    for t in range(60, 300, 40):
        snap = analyze(df.iloc[:t], CFG)
        assert snap.bar_index == t - 1
        all_indices = (
            [s.index for s in snap.swings]
            + [z.index for z in snap.fvgs]
            + [b.index for b in snap.order_blocks]
            + [ev.broken_index for ev in snap.structure]
            + [p.index for p in snap.liquidity]
        )
        assert all(0 <= i <= snap.bar_index for i in all_indices)


def test_short_history_returns_empty_snapshot():
    df = synthetic_ohlcv(20)  # < swing_length * 3
    snap = analyze(df, CFG)
    assert snap.swings == [] and snap.fvgs == []


# ------------------------------------------------------------ ручные FVG-кейсы


def test_fvg_detected_handcrafted():
    df = fvg_handcrafted()
    cfg = SmcConfig(swing_length=1)
    snap = analyze(df, cfg)
    bullish = [z for z in snap.fvgs if z.direction == "bullish"]
    assert len(bullish) == 1
    z = bullish[0]
    assert z.index == 1
    assert z.top == pytest.approx(12.0)
    assert z.bottom == pytest.approx(10.0)


def test_fvg_mitigation_is_prefix_relative():
    """До митигирующей свечи FVG активен; после — митигирован. Одна и та же
    зона, разные префиксы — семантика «что было известно в момент t»."""
    df = fvg_handcrafted()
    cfg = SmcConfig(swing_length=1)

    before = analyze(df.iloc[:5], cfg)  # бары 0-4, митигации еще нет
    zone_before = [z for z in before.fvgs if z.direction == "bullish"][0]
    assert not zone_before.mitigated
    assert zone_before in before.active_fvgs()

    after = analyze(df, cfg)  # бар 5 зашел в зону
    zone_after = [z for z in after.fvgs if z.direction == "bullish"][0]
    assert zone_after.mitigated
    assert zone_after not in after.active_fvgs()


def test_input_validation():
    with pytest.raises(ValueError, match="missing columns"):
        analyze(pd.DataFrame({"open": [1], "high": [1]}), CFG)
