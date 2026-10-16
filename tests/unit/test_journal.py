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


V3_COUNTERFACTUALS = """
CREATE TABLE counterfactuals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    cohort TEXT NOT NULL CHECK (cohort IN
        ('shadow','scout_pass','trader_decline','risk_rejected',
         'approval_rejected','approval_expired')),
    candidate_json TEXT NOT NULL,
    entry_price REAL NOT NULL,
    sl_price REAL NOT NULL,
    tp_price REAL NOT NULL,
    hypothetical_outcome TEXT CHECK (hypothetical_outcome IS NULL OR hypothetical_outcome IN
        ('tp_hit','sl_hit','expired_flat')),
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)
"""


def _v3_journal(db: Path) -> None:
    """Журнал v3 как на живой машине: counterfactuals со старым CHECK и данными."""
    import sqlite3

    connect(db).close()
    raw = sqlite3.connect(db)
    raw.execute("DROP TABLE counterfactuals")
    raw.execute(V3_COUNTERFACTUALS)
    raw.execute("CREATE INDEX idx_cf_custom ON counterfactuals(cohort)")  # чужой индекс выживет
    for i, cohort in enumerate(["shadow", "risk_rejected", "scout_pass"], 1):
        raw.execute(
            "INSERT INTO counterfactuals (decision_id, cohort, candidate_json, entry_price, "
            "sl_price, tp_price, hypothetical_outcome, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"d{i}", cohort, '{"symbol": "SOL/USDT:USDT"}', 100.0 + i, 101.5, 96.0,
             "tp_hit" if i == 1 else None, f"2026-10-1{i}T00:00:00.000Z"),
        )
    raw.execute("DELETE FROM counterfactuals WHERE decision_id='d3'")  # счетчик id ушел вперед
    raw.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    raw.commit()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        raw.execute("INSERT INTO counterfactuals (decision_id, cohort, candidate_json, entry_price, "
                    "sl_price, tp_price) VALUES ('x','executed','{}',1,1,1)")
    raw.close()


def test_migration_v3_to_v4_keeps_counterfactuals_and_accepts_new_cohorts(tmp_path: Path):
    """F4 (2026-10-16): CHECK cohort расширен пересозданием таблицы — данные, id,
    UNIQUE, индексы и счетчик AUTOINCREMENT на месте; executed/pending/error пишутся."""
    from oskar.journal.db import SCHEMA_VERSION

    db = tmp_path / "live.db"
    _v3_journal(db)
    conn = connect(db)
    assert SCHEMA_VERSION == 4
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
    rows = [tuple(r) for r in conn.execute(
        "SELECT id, decision_id, cohort, entry_price, hypothetical_outcome, created_at "
        "FROM counterfactuals ORDER BY id")]
    assert rows == [
        (1, "d1", "shadow", 101.0, "tp_hit", "2026-10-11T00:00:00.000Z"),
        (2, "d2", "risk_rejected", 102.0, None, "2026-10-12T00:00:00.000Z"),
    ]
    for cohort in ("executed", "pending", "error"):
        conn.execute(
            "INSERT INTO counterfactuals (decision_id, cohort, candidate_json, entry_price, "
            "sl_price, tp_price) VALUES (?,?,'{}',1,1,1)", (f"new-{cohort}", cohort),
        )
    conn.commit()
    new_ids = [r[0] for r in conn.execute("SELECT id FROM counterfactuals WHERE decision_id LIKE 'new-%'")]
    assert min(new_ids) == 4  # id 3 (удаленный) не переиспользован
    with pytest.raises(Exception, match="CHECK"):
        conn.execute("INSERT INTO counterfactuals (decision_id, cohort, candidate_json, entry_price, "
                     "sl_price, tp_price) VALUES ('bad','whatever','{}',1,1,1)")
    with pytest.raises(Exception, match="UNIQUE"):
        conn.execute("INSERT INTO counterfactuals (decision_id, cohort, candidate_json, entry_price, "
                     "sl_price, tp_price) VALUES ('d1','shadow','{}',1,1,1)")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE tbl_name='counterfactuals'")}
    assert "idx_cf_custom" in names and not any("__v" in n for n in names)
    conn.close()
    # повторное открытие — без миграции и без потерь
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM counterfactuals").fetchone()[0] == 5
    conn.close()


def test_migration_failure_rolls_back_whole_step(tmp_path: Path, monkeypatch):
    """Сбой посреди пересоздания (новая таблица уже создана, перенос упал) — журнал
    остается v3 со старой таблицей и данными, без брошенной временной таблицы."""
    import sqlite3

    import oskar.journal.db as jdb

    db = tmp_path / "live.db"
    _v3_journal(db)
    monkeypatch.setitem(
        jdb.TABLE_DDL, "counterfactuals",
        "CREATE TABLE {name} (id INTEGER PRIMARY KEY, decision_id TEXT CHECK (decision_id = 'never'))",
    )
    with pytest.raises(sqlite3.IntegrityError):
        connect(db)
    raw = sqlite3.connect(db)
    assert raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
    assert raw.execute("SELECT COUNT(*) FROM counterfactuals").fetchone()[0] == 2
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any("__v" in t for t in tables)
    raw.close()


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
