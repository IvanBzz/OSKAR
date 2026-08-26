"""Журнал OSKAR — SQLite (WAL). Схема портирована из v1 и расширена.

ПОМНИ (ст. 1): журнал — КЕШ. Биржа — правда. Каждая строка trades несет
client_order_id для точной реконсиляции; reconciled-флаг ставит sync/reconcile.py.

Таблицы:
- trades        — сделки: полный жизненный цикл + confidence, reasoning, close_reason
- decisions     — ВСЕ решения, включая отклоненные риск-лестницей (с причиной)
- analysis_log  — одна строка на LLM-вызов: полный промпт+ответ (реплей офлайн, ст. 1 п. 4)
- trade_lessons — уроки post-trade Analyst
- pending_approvals — карточки, ждущие Telegram-подтверждения (TTL)
- counterfactuals   — непроторгованные сетапы: что было бы (когорты take/pass/reject)
- sync_snapshots    — результаты реконсиляций
"""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,              -- канонический
    side TEXT NOT NULL CHECK (side IN ('long','short')),
    status TEXT NOT NULL CHECK (status IN ('pending','open','closed','cancelled')),
    entry_price REAL,
    exit_price REAL,
    qty REAL,
    leverage REAL,
    risk_usd REAL,
    sl_price REAL,
    tp_price REAL,
    sl_order_id TEXT,
    tp_order_id TEXT,
    confidence INTEGER,
    setup_type TEXT,
    timeframe TEXT,
    reasoning TEXT,
    close_reason TEXT CHECK (close_reason IS NULL OR close_reason IN
        ('sl_hit','tp_hit','trailing_or_partial','duration_timeout','panic','manual')),
    entry_fees REAL,
    exit_fees REAL,
    realized_pnl REAL,
    opened_at TEXT,
    closed_at TEXT,
    duration_seconds INTEGER,
    reconciled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_run ON trades(run_id);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    candidate_json TEXT NOT NULL,      -- SetupCandidate
    scout_json TEXT,                   -- ScoutReport (если дошло до Scout)
    trade_json TEXT,                   -- TradeDecision (если Scout сказал take)
    risk_json TEXT,                    -- RiskVerdict (всегда, если дошло до лестницы)
    outcome TEXT NOT NULL CHECK (outcome IN
        ('executed','risk_rejected','scout_pass','approval_rejected','approval_expired','error')),
    outcome_detail TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS analysis_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    agent TEXT NOT NULL CHECK (agent IN ('scout','trader','analyst','sentinel')),
    decision_id TEXT,
    model TEXT NOT NULL,
    prompt_sha TEXT NOT NULL,
    prompt TEXT NOT NULL,              -- ПОЛНЫЙ промпт (ст. 1 п. 4 — реплей)
    response TEXT NOT NULL,            -- полный ответ модели
    duration_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_analysis_sha ON analysis_log(prompt_sha);

CREATE TABLE IF NOT EXISTS trade_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id TEXT NOT NULL UNIQUE,
    trade_ids TEXT NOT NULL,           -- csv
    category TEXT NOT NULL,
    observation TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'hypothesis'
        CHECK (status IN ('hypothesis','validated','refuted')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS pending_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    card_json TEXT NOT NULL,           -- что показано в Telegram
    telegram_message_id TEXT,
    expires_at TEXT NOT NULL,          -- TTL: истек = reject (fail-closed)
    resolution TEXT CHECK (resolution IS NULL OR resolution IN
        ('approved','rejected','expired','snoozed')),
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS counterfactuals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    cohort TEXT NOT NULL CHECK (cohort IN
        ('shadow','scout_pass','risk_rejected','approval_rejected','approval_expired')),
    candidate_json TEXT NOT NULL,
    entry_price REAL NOT NULL,
    sl_price REAL NOT NULL,
    tp_price REAL NOT NULL,
    hypothetical_outcome TEXT CHECK (hypothetical_outcome IS NULL OR hypothetical_outcome IN
        ('tp_hit','sl_hit','expired_flat')),         -- NULL = еще отслеживается
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS sync_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('balance','positions','diff','reconcile')),
    payload_json TEXT NOT NULL,
    clean INTEGER,                     -- для diff/reconcile: 1 = расхождений нет
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Открыть журнал, применить схему, проставить версию.

    check_same_thread=False: демон один, но uvicorn исполняет эндпоинты в
    worker-потоках; WAL + сериализация записей через commit достаточны.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    existing = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if int(existing["value"]) != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"journal schema v{existing['value']} != code v{SCHEMA_VERSION}; "
            "run archive + migration first"
        )
    return conn
