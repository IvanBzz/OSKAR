"""exchange_sync: diff журнал-биржа, включая регресс на qty-баг v1."""

from pathlib import Path

from oskar.journal.db import connect
from oskar.sync.exchange_sync import (
    diff_positions,
    exchange_position_qty,
    render_diff,
    snapshot_balance,
)


class FakeView:
    def __init__(self, positions=None, bal=None):
        self._positions = positions or []
        self._bal = bal or {"USDT": {"total": 100.0, "free": 80.0, "used": 20.0}}

    def balance(self):
        return self._bal

    def positions(self):
        return self._positions

    def open_orders(self):
        return []


def journal_with_open(tmp_path: Path, rows):
    conn = connect(tmp_path / "j.db")
    for i, (symbol, side, qty) in enumerate(rows):
        conn.execute(
            "INSERT INTO trades (run_id, decision_id, client_order_id, symbol, side, "
            "status, qty) VALUES ('r1', ?, ?, ?, ?, 'open', ?)",
            (f"d{i}", f"OSKAR-r1-d{i}", symbol, side, qty),
        )
    conn.commit()
    return conn


def test_balance_snapshot():
    b = snapshot_balance(FakeView())
    assert b == {"currency": "USDT", "total": 100.0, "free": 80.0, "used": 20.0}


def test_balance_snapshot_vst_demo():
    """BingX VST держит демо-средства в валюте 'VST'."""
    view = FakeView(bal={"VST": {"total": 121.71, "free": 121.71, "used": 0.0}})
    b = snapshot_balance(view)
    assert b["currency"] == "VST" and b["total"] == 121.71


def test_contract_size_multiplication():
    """Гипотеза qty-бага v1: биржа отдает contracts, реальное qty = x contractSize."""
    assert exchange_position_qty({"contracts": 255.67, "contractSize": 6.981}) > 1700


def test_diff_clean(tmp_path):
    j = journal_with_open(tmp_path, [("SOL/USDT:USDT", "short", 5.0)])
    view = FakeView([{"symbol": "SOL-USDT", "side": "short", "contracts": 5.0, "contractSize": 1.0}])
    d = diff_positions(view, j)
    assert d.clean
    assert "DIFF CLEAN" in render_diff(d)
    j.close()


def test_diff_catches_v1_qty_bug(tmp_path):
    """Сценарий SUSHI из v1: журнал 255.67, биржа 1785 (ratio 6.98)."""
    j = journal_with_open(tmp_path, [("SUSHI/USDT:USDT", "short", 255.67)])
    view = FakeView([{"symbol": "SUSHI-USDT", "side": "short", "contracts": 1785.0, "contractSize": 1.0}])
    d = diff_positions(view, j)
    assert not d.clean
    q = d.qty_diffs[0]
    assert not q.ok and round(q.ratio, 2) == 6.98
    assert "MISMATCH" in render_diff(d)
    j.close()


def test_diff_symbol_format_immune(tmp_path):
    """ADA-USDT на бирже == ADA/USDT:USDT в журнале — НЕ расхождение (баг v1)."""
    j = journal_with_open(tmp_path, [("ADA/USDT:USDT", "short", 100.0)])
    view = FakeView([{"symbol": "ADA-USDT", "side": "short", "contracts": 100.0, "contractSize": 1.0}])
    d = diff_positions(view, j)
    assert d.clean
    j.close()


def test_diff_phantom_journal_position(tmp_path):
    """Фантом v1: сделка 'открыта' в журнале, биржа о ней не знает."""
    j = journal_with_open(tmp_path, [("ONE/USDT:USDT", "long", 20544.0)])
    d = diff_positions(FakeView([]), j)
    assert not d.clean
    assert d.missing_on_exchange == ["ONE/USDT:USDT long"]
    j.close()


def test_diff_untracked_exchange_position(tmp_path):
    """Обратный фантом: позиция на бирже, журнал пуст (вторая инстанция бота?)."""
    j = journal_with_open(tmp_path, [])
    view = FakeView([{"symbol": "BTC-USDT", "side": "long", "contracts": 0.01, "contractSize": 1.0}])
    d = diff_positions(view, j)
    assert not d.clean
    assert d.missing_in_journal == ["BTC/USDT:USDT long"]
    j.close()


def test_render_is_ascii(tmp_path):
    j = journal_with_open(tmp_path, [("SOL/USDT:USDT", "short", 5.0)])
    view = FakeView([{"symbol": "SOL-USDT", "side": "short", "contracts": 35.0, "contractSize": 1.0}])
    out = render_diff(diff_positions(view, j))
    out.encode("ascii")  # не бросает — вывод ASCII-only (cp1251 урок v1)
    j.close()
