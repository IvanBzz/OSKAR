"""Ревью 2026-10-13, E.4: SQL отбирает только неразрешенные исходы нужного символа.

2026-10-16 (R3): исходы — резолвером v2, свечи покрывают окно полностью."""

import json
from datetime import UTC, datetime

import pandas as pd
import pytest

from oskar.core.cycle import ShadowCycle
from oskar.core.state import RuntimeState
from oskar.journal.db import connect
from oskar.journal.episodes import HORIZON_BARS
from tests.unit.test_core import CAPS, PROFILE

SYMBOL = "SOL/USDT:USDT"
CREATED_MS = 1_791_889_200_000


class Cache:
    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    def get(self, symbol, timeframe, limit):
        self.calls.append(symbol)
        return self.bars


def add_counterfactual(journal, decision_id, symbol, direction="long", outcome=None):
    candidate = {
        "symbol": symbol, "direction": direction,
        "ts": datetime.fromtimestamp(CREATED_MS / 1000, UTC).isoformat(),
    }
    sl, tp = (98, 105) if direction == "long" else (102, 95)
    journal.execute(
        "INSERT INTO counterfactuals (decision_id, cohort, candidate_json, entry_price, "
        "sl_price, tp_price, hypothetical_outcome) VALUES (?, 'shadow', ?, 100, ?, ?, ?)",
        (decision_id, json.dumps(candidate), sl, tp, outcome),
    )


def flat_bars(n, last_high=101, last_low=99, low=99):
    """n баров 15м подряд от начала окна (CREATED_MS — граница бара): low-101, последний — свой."""
    highs = [101] * (n - 1) + [last_high]
    lows = [low] * (n - 1) + [last_low]
    return pd.DataFrame({
        "ts": [CREATED_MS + i * 900_000 for i in range(n)],
        "open": [100.0] * n, "high": highs, "low": lows, "close": [100.0] * n,
    })


@pytest.mark.parametrize(("direction", "high", "low", "bars", "base_low", "outcome"), [
    ("long", 106, 99, 2, 99, "tp_hit"),
    ("long", 106, 97, 2, 99, "sl_hit"),
    ("short", 101, 94, 2, 99, "tp_hit"),
    ("short", 103, 94, 2, 99, "sl_hit"),
    # ни разу не ниже входа 100 — по времени не режется (ревью 2026-10-16: duration-stop по
    # экстремуму бара, как тики монитора), горизонт закрывается по close
    ("long", 101, 100, HORIZON_BARS, 100, "horizon_close"),
])
def test_only_requested_unresolved_symbol_is_loaded(tmp_path, direction, high, low, bars, base_low, outcome):
    journal = connect(tmp_path / "j.db")
    add_counterfactual(journal, "target", SYMBOL, direction)
    add_counterfactual(journal, "resolved", SYMBOL, direction, "tp_hit")
    for i in range(100):
        add_counterfactual(journal, f"other-{i}", "BTC/USDT:USDT")
    journal.commit()
    returned = []
    original_factory = journal.row_factory

    def track_rows(cursor, row):
        if [column[0] for column in cursor.description] == [
            "id", "decision_id", "candidate_json", "entry_price", "sl_price", "tp_price",
        ]:
            returned.append(row[1])
        return original_factory(cursor, row)

    journal.row_factory = track_rows
    cache = Cache(flat_bars(bars, high, low, low=base_low))
    cycle = ShadowCycle(cache, RuntimeState(journal=journal), CAPS, PROFILE)
    # от самой старой висящей строки: target вставлен первым
    assert cycle.unresolved_symbols() == [SYMBOL, "BTC/USDT:USDT"]
    assert cycle.resolve_counterfactuals(SYMBOL) == 1
    assert returned == ["target"] and cache.calls == [SYMBOL]
    outcomes = dict(journal.execute("SELECT decision_id, hypothetical_outcome FROM counterfactuals"))
    assert outcomes["target"] == outcome and outcomes["resolved"] == "tp_hit"
    assert all(outcomes[f"other-{i}"] is None for i in range(100))
    assert cycle.unresolved_symbols() == ["BTC/USDT:USDT"]
    assert cycle.resolve_counterfactuals(SYMBOL) == 0 and cache.calls == [SYMBOL]
    journal.close()


def test_no_matching_symbol_does_not_fetch_candles(tmp_path):
    journal = connect(tmp_path / "j.db")
    add_counterfactual(journal, "other", "BTC/USDT:USDT")
    journal.commit()
    cache = Cache(None)
    cycle = ShadowCycle(cache, RuntimeState(journal=journal), CAPS, PROFILE)
    assert cycle.resolve_counterfactuals(SYMBOL) == 0
    assert cycle.resolve_counterfactuals("' OR 1=1 --") == 0
    assert cache.calls == []
    journal.close()
