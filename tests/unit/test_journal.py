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
    UNIQUE, индексы и счетчик AUTOINCREMENT на месте; executed/pending/error пишутся.
    С v5 журнал v3 проходит обе ступени одной транзакцией."""
    from oskar.journal.db import SCHEMA_VERSION

    db = tmp_path / "live.db"
    _v3_journal(db)
    conn = connect(db)
    assert SCHEMA_VERSION == 5
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "5"
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


V4_COUNTERFACTUALS = """
CREATE TABLE counterfactuals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    cohort TEXT NOT NULL CHECK (cohort IN
        ('shadow','scout_pass','trader_decline','risk_rejected',
         'approval_rejected','approval_expired',
         'executed','pending','error')),
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


def _cand(symbol: str, direction: str, ts: str) -> str:
    import json

    return json.dumps({"symbol": symbol, "direction": direction, "ts": ts, "timeframe": "15m"})


def _v4_journal(db: Path) -> None:
    """Журнал v4 как на живой машине (блок F): исходы v1, висящая строка, две серии."""
    import sqlite3

    connect(db).close()
    raw = sqlite3.connect(db)
    raw.execute("DROP TABLE counterfactuals")
    raw.execute(V4_COUNTERFACTUALS)
    rows = [
        ("d1", "shadow", _cand("SOL/USDT:USDT", "long", "2026-10-15T10:00:05Z"), "tp_hit"),
        ("d2", "executed", _cand("SOL/USDT:USDT", "long", "2026-10-15T10:30:07Z"), "expired_flat"),
        ("d3", "risk_rejected", _cand("SOL/USDT:USDT", "long", "2026-10-15T23:00:00Z"), None),
        ("d4", "shadow", _cand("SOL/USDT:USDT", "short", "2026-10-15T10:30:00Z"), "sl_hit"),
        ("d5", "shadow", '{"symbol": "BTC/USDT:USDT"}', None),  # без ts/direction — эпизода нет
    ]
    for did, cohort, cj, outcome in rows:
        raw.execute(
            "INSERT INTO counterfactuals (decision_id, cohort, candidate_json, entry_price, "
            "sl_price, tp_price, hypothetical_outcome, resolved_at) VALUES (?,?,?,100,98,104,?,?)",
            (did, cohort, cj, outcome, "2026-10-16T00:00:00.000Z" if outcome else None),
        )
    raw.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    raw.commit()
    raw.close()


def test_migration_v4_to_v5_keeps_v1_outcomes_and_adds_resolver_columns(tmp_path: Path):
    """R3 (2026-10-16): CHECK исходов расширен (no_data/unfilled/duration_stop/horizon_close),
    старые строки — resolver_version 1, их исходы не переписаны, эпизоды проставлены."""
    from oskar.journal.db import SCHEMA_VERSION

    db = tmp_path / "live.db"
    _v4_journal(db)
    conn = connect(db)
    assert SCHEMA_VERSION == 5
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "5"
    rows = {r["decision_id"]: r for r in conn.execute("SELECT * FROM counterfactuals")}
    assert [rows[d]["hypothetical_outcome"] for d in ("d1", "d2", "d3", "d4", "d5")] == [
        "tp_hit", "expired_flat", None, "sl_hit", None]
    assert {r["resolver_version"] for r in rows.values()} == {1}
    assert all(r["realized_r"] is None and r["outcome_detail"] is None for r in rows.values())
    assert rows["d1"]["resolved_at"] == "2026-10-16T00:00:00.000Z"
    # серия: d1 и d2 (через 30 мин) — один эпизод; d3 через 13 ч от начала — новый; short — свой
    ep = {d: rows[d]["episode_id"] for d in rows}
    assert ep["d1"] == ep["d2"] == "SOL/USDT:USDT|long|2026-10-15T10:00:05Z"
    assert ep["d3"] == "SOL/USDT:USDT|long|2026-10-15T23:00:00Z"
    assert ep["d4"] == "SOL/USDT:USDT|short|2026-10-15T10:30:00Z" and ep["d5"] is None
    for outcome in ("no_data", "unfilled", "duration_stop", "horizon_close", "tp_hit", "expired_flat"):
        conn.execute(
            "INSERT INTO counterfactuals (decision_id, cohort, candidate_json, entry_price, sl_price, "
            "tp_price, hypothetical_outcome, resolver_version) VALUES (?, 'shadow', '{}', 1, 1, 1, ?, 2)",
            (f"new-{outcome}", outcome),
        )
    with pytest.raises(Exception, match="CHECK"):
        conn.execute("INSERT INTO counterfactuals (decision_id, cohort, candidate_json, entry_price, "
                     "sl_price, tp_price, hypothetical_outcome) VALUES ('bad','shadow','{}',1,1,1,'flat')")
    conn.commit()
    conn.close()
    conn = connect(db)  # повторное открытие — без миграции и без потерь
    assert conn.execute("SELECT COUNT(*) FROM counterfactuals").fetchone()[0] == 11
    conn.close()


def test_migration_v4_to_v5_failure_rolls_back(tmp_path: Path, monkeypatch):
    """Сбой в переносе данных v5 (таблица уже пересоздана) — журнал остается v4: старая
    таблица со старым CHECK, данные и исходы на месте, временной таблицы нет."""
    import sqlite3

    import oskar.journal.db as jdb

    db = tmp_path / "live.db"
    _v4_journal(db)

    def boom(conn):
        raise RuntimeError("backfill failed")

    monkeypatch.setitem(jdb.BACKFILLS, 5, [boom])
    with pytest.raises(RuntimeError, match="backfill failed"):
        connect(db)
    raw = sqlite3.connect(db)
    assert raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
    cols = {r[1] for r in raw.execute("PRAGMA table_info(counterfactuals)")}
    assert "resolver_version" not in cols and "episode_id" not in cols
    assert raw.execute(
        "SELECT decision_id, hypothetical_outcome FROM counterfactuals ORDER BY id").fetchall() == [
        ("d1", "tp_hit"), ("d2", "expired_flat"), ("d3", None), ("d4", "sl_hit"), ("d5", None)]
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any("__v" in t for t in tables)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # CHECK v4 на месте
        raw.execute("INSERT INTO counterfactuals (decision_id, cohort, candidate_json, entry_price, "
                    "sl_price, tp_price, hypothetical_outcome) VALUES ('x','shadow','{}',1,1,1,'no_data')")
    raw.close()
    monkeypatch.undo()
    conn = connect(db)  # после устранения сбоя миграция проходит
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "5"
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
