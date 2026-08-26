"""Ядро: API (TestClient), port-lock, shadow-цикл на синтетике."""

import socket
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from oskar.core.api import build_app
from oskar.core.cycle import CycleConfig, ShadowCycle
from oskar.core.daemon import SingleInstanceError, check_port_free
from oskar.core.state import RuntimeState
from oskar.detect.smc import SmcConfig
from oskar.journal.db import connect
from oskar.risk.ladder import Caps, ProfileLimits

CAPS = Caps(
    risk_per_trade_pct=1.0, max_total_risk_pct=5.0, max_leverage=10,
    confidence_cap=75, min_rr_fee_adjusted=2.0, max_open_positions=3,
)
PROFILE = ProfileLimits(sl_dist_pct_min=0.3, sl_dist_pct_max=6.0, max_leverage=8)


@pytest.fixture
def state(tmp_path):
    return RuntimeState(journal=connect(tmp_path / "j.db"))


@pytest.fixture
def client(state):
    return TestClient(build_app(state))


def add_pending(state, decision_id="d1", expires_in_min=10):
    expires = (datetime.now(UTC) + timedelta(minutes=expires_in_min)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"
    state.journal.execute(
        "INSERT INTO pending_approvals (decision_id, card_json, expires_at) VALUES (?,?,?)",
        (decision_id, "{}", expires),
    )
    state.journal.commit()


# ------------------------------------------------------------------------ api


def test_status(client, state):
    r = client.get("/status").json()
    assert r["run_id"] == state.run_id
    assert r["paused"] is False and r["safe_mode"] is False


def test_approve_flow(client, state):
    add_pending(state, "d1")
    assert client.get("/pending").json()[0]["decision_id"] == "d1"
    assert client.post("/decisions/d1/approve").json()["resolution"] == "approved"
    assert client.get("/pending").json() == []
    # повторное разрешение отвергается
    assert client.post("/decisions/d1/reject").status_code == 409


def test_approve_unknown_404(client):
    assert client.post("/decisions/nope/approve").status_code == 404


def test_expired_ttl_fail_closed(client, state):
    add_pending(state, "old", expires_in_min=-5)
    r = client.post("/decisions/old/approve")
    assert r.status_code == 409 and "expired" in r.json()["detail"]
    row = state.journal.execute(
        "SELECT resolution FROM pending_approvals WHERE decision_id='old'"
    ).fetchone()
    assert row["resolution"] == "expired"


def test_pause_blocks_approvals(client, state):
    add_pending(state, "d2")
    client.post("/pause")
    assert client.post("/decisions/d2/approve").status_code == 423
    assert client.post("/decisions/d2/reject").status_code == 200  # reject разрешен всегда


def test_panic_and_no_auto_resume(client, state):
    r = client.post("/panic").json()
    assert r == {"panic": True, "safe_mode": True, "paused": True}
    # после panic /resume отказывает — только ручной сброс оператором (ст. 6.3)
    assert client.post("/resume").status_code == 423


# ------------------------------------------------------------------ port lock


def test_port_lock_detects_second_instance():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    try:
        with pytest.raises(SingleInstanceError, match="another oskard"):
            check_port_free("127.0.0.1", port)
    finally:
        s.close()
    check_port_free("127.0.0.1", port)  # порт освобожден — проходит


# --------------------------------------------------------------- shadow cycle


def market_df(n: int = 160, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    legs = np.concatenate([
        np.linspace(100, 112, 40), np.linspace(112, 104, 30),
        np.linspace(104, 118, 45), np.linspace(118, 108, 45),
    ])[:n]
    close = legs + rng.normal(0, 0.35, n)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    spread = np.abs(rng.normal(0, 0.3, n)) + 0.1
    ts = 1_700_000_000_000 + np.arange(n) * 900_000
    return pd.DataFrame({
        "ts": ts, "open": open_, "high": np.maximum(open_, close) + spread,
        "low": np.minimum(open_, close) - spread, "close": close,
        "volume": np.abs(rng.normal(1000, 300, n)),
    })


class FakeCache:
    def __init__(self, df):
        self._df = df

    def get(self, symbol, timeframe, limit):
        return self._df.copy()


def make_cycle(state):
    cfg = CycleConfig(
        signal_timeframe="15m", context_timeframes=(),
        min_score=30, smc=SmcConfig(swing_length=5),
    )
    # префикс 130 бар от полной серии — состояние со свежей структурой (см. replay)
    prefix = market_df().iloc[:130].reset_index(drop=True)
    return ShadowCycle(FakeCache(prefix), state, CAPS, PROFILE, cfg)


def test_shadow_cycle_journals_decisions(state):
    cycle = make_cycle(state)
    report = cycle.run_once(["SOL/USDT:USDT"], equity_usd=1000.0)
    assert report.scanned == 1 and not report.errors
    assert report.candidates >= 1
    n_dec = state.journal.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    n_cf = state.journal.execute("SELECT COUNT(*) FROM counterfactuals").fetchone()[0]
    assert n_dec == n_cf == report.candidates


def test_shadow_cycle_idempotent_within_cooldown(state):
    cycle = make_cycle(state)
    cycle.run_once(["SOL/USDT:USDT"], 1000.0)
    n1 = state.journal.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    cycle.run_once(["SOL/USDT:USDT"], 1000.0)  # те же свечи — cooldown молчит
    n2 = state.journal.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert n1 == n2


def test_shadow_cycle_respects_pause(state):
    cycle = make_cycle(state)
    state.paused = True
    report = cycle.run_once(["SOL/USDT:USDT"], 1000.0)
    assert report.scanned == 0


def test_shadow_cycle_symbol_error_isolated(state):
    class BrokenCache:
        def get(self, *a):
            raise ConnectionError("boom")

    cfg = CycleConfig(min_score=30, context_timeframes=())
    cycle = ShadowCycle(BrokenCache(), state, CAPS, PROFILE, cfg)
    report = cycle.run_once(["SOL/USDT:USDT"], 1000.0)
    assert report.errors and "ConnectionError" in report.errors[0]
