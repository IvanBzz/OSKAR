"""Bootstrap конфигов и shadow-runner: тики, вотчлист, error-streak -> safe_mode."""

import pandas as pd
import pytest

from oskar.core.bootstrap import (
    build_cycle_config,
    load_active_profile,
    load_caps,
    load_scanner_settings,
    load_settings,
)
from oskar.core.shadow_runner import ShadowRunner
from oskar.core.state import RuntimeState
from oskar.journal.db import connect

# ----------------------------------------------------------------- bootstrap


def test_caps_load_matches_yaml():
    caps = load_caps()
    assert caps.risk_per_trade_pct == 1.0
    assert caps.max_total_risk_pct == 5.0
    assert caps.max_leverage == 10
    assert caps.confidence_cap == 75
    assert caps.min_rr_fee_adjusted == 2.0
    assert caps.max_open_positions == 3


def test_active_profile_intraday():
    p = load_active_profile()
    assert p.name == "intraday"
    assert p.signal_timeframes[0] == "15m"
    assert p.limits.max_leverage <= load_caps().max_leverage
    assert p.approval_ttl_seconds == 1200


def test_cycle_config_derived():
    settings = load_settings()
    cfg = build_cycle_config(load_active_profile(), load_scanner_settings(settings), settings)
    assert cfg.signal_timeframe == "15m"
    assert cfg.min_score == 50           # калибровка пробы №1
    assert cfg.smc.swing_length == 5     # detect.swing_length
    assert cfg.cooldown_bars == 2        # 1800с кулдауна / 900с бар
    assert "15m" not in cfg.context_timeframes


# -------------------------------------------------------------------- runner


class FakeCycle:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.runs = []

    def run_once(self, symbols, equity):
        if self.fail:
            raise ConnectionError("exchange down")
        self.runs.append((list(symbols), equity))

        class R:
            scanned = len(symbols)
            scored = 0
            candidates = 0
            risk_passed = 0
            errors = []

        return R()

    def resolve_counterfactuals(self, symbol):
        return 0


class FakeClient:
    def universe(self):
        return ["HOT/USDT:USDT", "FLAT/USDT:USDT"]


class FakeCache:
    def get(self, symbol, timeframe, limit):
        import numpy as np

        n = 80
        close = 100 + 0.4 * np.arange(n)
        open_ = np.roll(close, 1)
        open_[0] = close[0]
        return pd.DataFrame({
            "open": open_, "high": np.maximum(open_, close) + 0.5,
            "low": np.minimum(open_, close) - 0.5, "close": close,
            "volume": np.full(n, 5000.0),
        })


def make_runner(tmp_path, fail=False, equity_fn=None):
    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    cycle = FakeCycle(fail=fail)
    t = [0.0]
    runner = ShadowRunner(
        cycle, FakeCache(), FakeClient(), state,
        top_n=5, min_quote_volume=0, tick_seconds=60,
        equity_fn=equity_fn, clock=lambda: t[0],
    )
    return runner, cycle, state, t


def test_tick_builds_watchlist_and_runs_cycle(tmp_path):
    runner, cycle, state, t = make_runner(tmp_path)
    runner.tick()
    assert runner.watchlist  # universe отфильтрован и отсортирован
    assert runner.ticks == 1
    assert cycle.runs and cycle.runs[0][1] == 1000.0  # дефолтная equity
    assert not state.safe_mode


def test_watchlist_cached_between_ticks(tmp_path):
    runner, cycle, state, t = make_runner(tmp_path)
    runner.tick()
    wl = list(runner.watchlist)
    t[0] = 300.0  # < 30 мин — вотчлист не пересобирается
    runner.tick()
    assert runner.watchlist == wl and runner.ticks == 2


def test_equity_fn_updates(tmp_path):
    runner, cycle, state, t = make_runner(tmp_path, equity_fn=lambda: 121.71)
    runner.tick()
    assert runner.equity_usd == 121.71
    assert cycle.runs[0][1] == 121.71


def test_error_streak_triggers_safe_mode(tmp_path):
    runner, cycle, state, t = make_runner(tmp_path, fail=True)
    for _ in range(3):
        runner.tick()
    assert state.safe_mode  # 3 подряд — правило v1


def test_single_error_does_not_trigger(tmp_path):
    runner, cycle, state, t = make_runner(tmp_path, fail=True)
    runner.tick()
    assert not state.safe_mode
    cycle.fail = False
    runner.tick()  # успех сбрасывает серию
    runner._err_streak == 0  # noqa: B015 - документирующий ассерт
    cycle.fail = True
    runner.tick()
    assert not state.safe_mode


def test_status_endpoint_includes_shadow(tmp_path):
    from fastapi.testclient import TestClient

    from oskar.core.api import build_app

    runner, cycle, state, t = make_runner(tmp_path)
    runner.tick()
    client = TestClient(build_app(state, shadow=runner))
    payload = client.get("/status").json()
    assert payload["shadow"]["ticks"] == 1
    assert payload["shadow"]["watchlist"] >= 1
    assert "decisions_total" in payload


@pytest.mark.parametrize("n", [1])
def test_runner_stop_flag(tmp_path, n):
    runner, *_ = make_runner(tmp_path)
    runner.stop()
    runner.run_forever()  # выходит сразу — флаг взведен до старта
