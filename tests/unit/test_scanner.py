"""Сканер: ранжирование quick-скоринга, пропуск битых символов, top-N."""

import numpy as np
import pandas as pd

from oskar.data.scanner import quick_filter, quick_score_one


def df_trending(n: int = 80, vol_surge: bool = True) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    close = 100 + 0.4 * np.arange(n) + rng.normal(0, 0.2, n)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + 0.6
    low = np.minimum(open_, close) - 0.6
    vol = np.full(n, 1000.0)
    if vol_surge:
        vol[-1] = 2500.0
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol})


def df_flat(n: int = 80) -> pd.DataFrame:
    rng = np.random.default_rng(2)
    close = 100 + rng.normal(0, 0.05, n)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    return pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close) + 0.02,
        "low": np.minimum(open_, close) - 0.02, "close": close,
        "volume": np.full(n, 1000.0),
    })


class FakeCache:
    def __init__(self, data: dict[str, pd.DataFrame]):
        self._data = data
        self.calls: list[str] = []

    def get(self, symbol, timeframe, limit):
        self.calls.append(symbol)
        return self._data[symbol]


def test_trending_scores_above_flat():
    cache = FakeCache({"HOT/USDT:USDT": df_trending(), "FLAT/USDT:USDT": df_flat()})
    hot = quick_score_one(cache, "HOT/USDT:USDT")
    flat = quick_score_one(cache, "FLAT/USDT:USDT")
    assert hot.score > flat.score
    assert hot.volume_ratio >= 2.0
    assert hot.trend == "bullish"


def test_broken_symbol_skipped_not_fatal():
    cache = FakeCache({
        "OK/USDT:USDT": df_trending(),
        "SHORT/USDT:USDT": df_trending(20),  # < 60 свечей -> compute бросает
    })
    result = quick_filter(cache, ["OK/USDT:USDT", "SHORT/USDT:USDT", "MISSING/USDT:USDT"])
    assert [q.symbol for q in result] == ["OK/USDT:USDT"]


def test_top_n_and_deterministic_order():
    data = {f"S{i}/USDT:USDT": df_trending() for i in range(5)}
    data["FLAT/USDT:USDT"] = df_flat()
    cache = FakeCache(data)
    result = quick_filter(cache, sorted(data), top_n=3)
    assert len(result) == 3
    # одинаковые скоры -> алфавитный порядок, флэт не попал
    assert [q.symbol for q in result] == ["S0/USDT:USDT", "S1/USDT:USDT", "S2/USDT:USDT"]


def test_quick_filter_empty_universe():
    assert quick_filter(FakeCache({}), []) == []
