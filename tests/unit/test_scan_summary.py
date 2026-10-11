"""Сканер: сводка ошибок и остановка на cooldown (ошибки больше не глотаются молча)."""

import numpy as np
import pandas as pd

from oskar.data.client import ExchangeCooldown
from oskar.data.scanner import scan


def df_ok(n: int = 80) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    close = 100 + 0.4 * np.arange(n) + rng.normal(0, 0.2, n)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    return pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close) + 0.6,
        "low": np.minimum(open_, close) - 0.6, "close": close,
        "volume": np.full(n, 1000.0),
    })


class FakeCache:
    def __init__(self, data: dict):
        self._data = data  # symbol -> DataFrame | Exception
        self.calls: list[str] = []

    def get(self, symbol, timeframe, limit):
        self.calls.append(symbol)
        v = self._data[symbol]
        if isinstance(v, Exception):
            raise v
        return v


def test_summary_counts_scored_illiquid_and_errors():
    cache = FakeCache({
        "OK/USDT:USDT": df_ok(),
        "THIN/USDT:USDT": df_ok(),           # отрезан полом ликвидности
        "SHORT/USDT:USDT": df_ok(20),        # compute бросает: мало истории
        "DEAD/USDT:USDT": RuntimeError("DEAD/USDT:USDT is blacklisted (prior exchange error on klines)"),
    })
    # OK проходит пол 0; THIN проверяем отдельным вызовом с высоким полом
    top, summary = scan(cache, ["OK/USDT:USDT", "SHORT/USDT:USDT", "DEAD/USDT:USDT"], min_quote_volume=0)
    assert [q.symbol for q in top] == ["OK/USDT:USDT"]
    assert summary.total == 3 and summary.scored == 1 and summary.illiquid == 0
    assert summary.errors == {"ValueError": 1, "RuntimeError": 1} or sum(summary.errors.values()) == 2
    assert summary.aborted is None
    assert any("DEAD/USDT:USDT" in s for s in summary.samples)
    assert "errors 2" in summary.as_log()

    _, s2 = scan(cache, ["THIN/USDT:USDT"], min_quote_volume=10**9)
    assert s2.illiquid == 1 and s2.scored == 0


def test_scan_stops_on_cooldown_and_reports_partial():
    """ExchangeCooldown — пауза всего эндпоинта: дальше не перебираем,
    топ помечен частичным, оставшиеся символы до кеша не дошли."""
    cache = FakeCache({
        "A/USDT:USDT": df_ok(),
        "B/USDT:USDT": ExchangeCooldown(540.0, "exchange ban 109429"),
        "C/USDT:USDT": df_ok(),
    })
    top, summary = scan(cache, ["A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT"], min_quote_volume=0)
    assert [q.symbol for q in top] == ["A/USDT:USDT"]
    assert summary.aborted is not None and "109429" in summary.aborted
    assert cache.calls == ["A/USDT:USDT", "B/USDT:USDT"]  # C не запрашивался
    assert summary.scored == 1
