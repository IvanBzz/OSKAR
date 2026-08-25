"""Журнал: схема, ограничения CHECK, архивирование."""

from pathlib import Path

import pytest

from oskar.journal.archive import archive_journal
from oskar.journal.db import connect


def test_connect_creates_schema(tmp_path: Path):
    conn = connect(tmp_path / "j.db")
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert {
        "trades", "decisions", "analysis_log", "trade_lessons",
        "pending_approvals", "counterfactuals", "sync_snapshots", "meta",
    } <= tables


def test_close_reason_unknown_is_rejected(tmp_path: Path):
    """Категории 'unknown' НЕ СУЩЕСТВУЕТ в схеме (v1: 76% close_reason=unknown).
    Резолвер обязан выдать одну из явных категорий."""
    conn = connect(tmp_path / "j.db")
    with pytest.raises(Exception, match="CHECK"):
        conn.execute(
            """INSERT INTO trades (run_id, decision_id, client_order_id, symbol,
               side, status, close_reason)
               VALUES ('r1','d1','OSKAR-r1-d1','SOL/USDT:USDT','short','closed','unknown')"""
        )
    conn.close()


def test_decisions_outcome_enum(tmp_path: Path):
    conn = connect(tmp_path / "j.db")
    conn.execute(
        """INSERT INTO decisions (run_id, decision_id, candidate_json, outcome)
           VALUES ('r1','d1','{}','risk_rejected')"""
    )
    with pytest.raises(Exception, match="CHECK"):
        conn.execute(
            """INSERT INTO decisions (run_id, decision_id, candidate_json, outcome)
               VALUES ('r1','d2','{}','whatever')"""
        )
    conn.close()


def test_duplicate_decision_id_rejected(tmp_path: Path):
    conn = connect(tmp_path / "j.db")
    row = ("r1", "d1", "OSKAR-r1-d1", "SOL/USDT:USDT", "short", "open")
    conn.execute(
        "INSERT INTO trades (run_id, decision_id, client_order_id, symbol, side, status) "
        "VALUES (?,?,?,?,?,?)", row,
    )
    with pytest.raises(Exception, match="UNIQUE"):
        conn.execute(
            "INSERT INTO trades (run_id, decision_id, client_order_id, symbol, side, status) "
            "VALUES (?,?,?,?,?,?)", row,
        )
    conn.close()


def test_archive_creates_snapshot(tmp_path: Path):
    db = tmp_path / "j.db"
    conn = connect(db)
    conn.execute(
        "INSERT INTO decisions (run_id, decision_id, candidate_json, outcome) "
        "VALUES ('r1','d1','{}','executed')"
    )
    conn.commit()
    conn.close()

    snap = archive_journal(db)
    assert snap.exists() and snap.parent.name == "archive"

    import sqlite3

    check = sqlite3.connect(snap)
    n = check.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    check.close()
    assert n == 1
