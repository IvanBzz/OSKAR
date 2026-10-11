"""Shadow-runner под cooldown kline-API: цикл и пересборка вотчлиста ждут паузу,
прерванный скан не портит вотчлист, /status показывает здоровье данных."""

import numpy as np
import pandas as pd

from oskar.core.shadow_runner import REFRESH_TOP_SEC, ShadowRunner
from oskar.core.state import RuntimeState
from oskar.data.client import ExchangeCooldown
from oskar.journal.db import connect


class FakeCycle:
    def __init__(self):
        self.runs = []

    def run_once(self, symbols, equity):
        self.runs.append(list(symbols))

        class R:
            scanned = len(symbols)
            scored = candidates = risk_passed = 0
            errors = []

        return R()

    def resolve_counterfactuals(self, symbol):
        return 0


class FakeClient:
    """Клиент с управляемым cooldown (как MarketDataClient после 109429)."""

    def __init__(self, universe):
        self._universe = universe
        self.cooldown = 0.0
        self.cooldown_reason = ""

    def universe(self):
        return self._universe

    def cooldown_remaining(self):
        return self.cooldown

    def health(self):
        return {"universe": len(self._universe), "dead_symbols": 1, "cooldown_sec": int(self.cooldown)}


def df_ok(n: int = 80) -> pd.DataFrame:
    close = 100 + 0.4 * np.arange(n)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    return pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close) + 0.5,
        "low": np.minimum(open_, close) - 0.5, "close": close,
        "volume": np.full(n, 5000.0),
    })


class FakeCache:
    def __init__(self):
        self.fail_from: str | None = None  # с этого символа кеш бросает cooldown

    def get(self, symbol, timeframe, limit):
        if self.fail_from is not None and symbol >= self.fail_from:
            raise ExchangeCooldown(300.0, "error budget 6/6 per 15m")
        return df_ok()


def make_runner(tmp_path, universe=("A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT")):
    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    cycle, cache, client = FakeCycle(), FakeCache(), FakeClient(list(universe))
    t = [0.0]
    runner = ShadowRunner(
        cycle, cache, client, state, top_n=5, min_quote_volume=0, tick_seconds=60,
        clock=lambda: t[0],
    )
    return runner, cycle, cache, client, state, t


def test_cooldown_postpones_refresh_and_skips_cycle(tmp_path):
    runner, cycle, cache, client, state, t = make_runner(tmp_path)
    client.cooldown = 120.0
    runner.tick()
    assert runner.watchlist == [] and cycle.runs == []  # ни скана, ни цикла
    assert runner.ticks == 0 and not state.safe_mode  # и это не «ошибка инфраструктуры»

    t[0] = 60.0  # пауза еще идет — ничего не меняется
    runner.tick()
    assert cycle.runs == []

    client.cooldown = 0.0
    t[0] = 122.0  # пауза кончилась: пересборка должна случиться СРАЗУ, не через 30 мин
    runner.tick()
    assert runner.watchlist == ["A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT"]
    assert cycle.runs == [runner.watchlist] and runner.ticks == 1


def test_aborted_scan_keeps_previous_watchlist(tmp_path):
    runner, cycle, cache, client, state, t = make_runner(tmp_path)
    runner.tick()
    assert len(runner.watchlist) == 3

    cache.fail_from = "B/USDT:USDT"  # скан падает в cooldown на втором символе
    client.cooldown = 0.0  # ...а клиент узнает о паузе уже в процессе скана
    t[0] = REFRESH_TOP_SEC + 1
    runner.tick()
    assert runner.watchlist == ["A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT"]  # частичный топ отвергнут
    assert not state.safe_mode


def test_aborted_scan_on_cold_start_uses_partial_top(tmp_path):
    runner, cycle, cache, client, state, t = make_runner(tmp_path)
    cache.fail_from = "B/USDT:USDT"
    runner.tick()
    assert runner.watchlist == ["A/USDT:USDT"]  # лучше перекошенный, чем пустой


def test_status_exposes_data_health(tmp_path):
    from fastapi.testclient import TestClient

    from oskar.core.api import build_app

    runner, cycle, cache, client, state, t = make_runner(tmp_path)
    runner.tick()
    client.cooldown = 42.0
    payload = TestClient(build_app(state, shadow=runner)).get("/status").json()
    assert payload["data"] == {"universe": 3, "dead_symbols": 1, "cooldown_sec": 42}
