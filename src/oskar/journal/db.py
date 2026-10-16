"""Журнал OSKAR — SQLite (WAL). Схема портирована из v1 и расширена.

ПОМНИ (ст. 1): журнал — КЕШ. Биржа — правда. Каждая строка trades несет
client_order_id для точной реконсиляции; reconciled-флаг ставит sync/reconcile.py.

Таблицы:
- trades        — сделки: полный жизненный цикл + confidence, reasoning, close_reason
- decisions     — ВСЕ решения, включая отклоненные риск-лестницей (с причиной)
- analysis_log  — одна строка на LLM-вызов: полный промпт+ответ (реплей офлайн, ст. 1 п. 4)
- trade_lessons — уроки post-trade Analyst
- pending_approvals — карточки, ждущие Telegram-подтверждения (TTL)
- counterfactuals   — сетапы и их гипотетический исход (когорты shadow/pass/reject/...,
                      с v4 — и эскалированные executed/pending/error по выбранной комбинации)
- sync_snapshots    — результаты реконсиляций
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 4  # v4: counterfactuals.cohort + executed/pending/error (2026-10-16)

# Миграции «вперед»: версия -> ADD COLUMN (имя, определение). Журнал хранит
# датасет решений и counterfactuals — стирать его при смене схемы нельзя
# (ревью 2026-10-13: архив+удаление уносили и открытые сделки).
MIGRATIONS: dict[int, list[tuple[str, str, str]]] = {
    3: [
        ("trades", "entry_order_id", "TEXT"),
        ("trades", "entry_type", "TEXT"),
    ],
    4: [],  # только пересоздание таблицы (REBUILDS)
}

# Миграции «вперед», которые ALTER не умеет (CHECK в SQLite не меняется): версия ->
# таблицы, пересоздаваемые по актуальному DDL с переносом данных (_rebuild_table).
# 2026-10-16, v4: CHECK counterfactuals.cohort не пускал executed/pending/error —
# эскалированные сетапы писались в 'shadow' вперемешку с чистым shadow.
REBUILDS: dict[int, list[str]] = {
    4: ["counterfactuals"],
}

COUNTERFACTUALS_DDL = """
CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    cohort TEXT NOT NULL CHECK (cohort IN
        ('shadow','scout_pass','trader_decline','risk_rejected',
         'approval_rejected','approval_expired',
         'executed','pending','error')),             -- v4: эскалированные сетапы
    candidate_json TEXT NOT NULL,
    entry_price REAL NOT NULL,
    sl_price REAL NOT NULL,
    tp_price REAL NOT NULL,
    hypothetical_outcome TEXT CHECK (hypothetical_outcome IS NULL OR hypothetical_outcome IN
        ('tp_hit','sl_hit','expired_flat')),         -- NULL = еще отслеживается
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""

# DDL таблиц из REBUILDS: {name} — имя (временное при пересоздании)
TABLE_DDL: dict[str, str] = {"counterfactuals": COUNTERFACTUALS_DDL}

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
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    entry_order_id TEXT,               -- v3: биржевой id входа (опрос по id, не по clientOrderId)
    entry_type TEXT                    -- v3: market | limit (TTL неизвестного входа)
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
        ('executed','risk_rejected','scout_pass','trader_decline','pending',
         'approval_rejected','approval_expired','error')),
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

CREATE TABLE IF NOT EXISTS sync_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('balance','positions','diff','reconcile')),
    payload_json TEXT NOT NULL,
    clean INTEGER,                     -- для diff/reconcile: 1 = расхождений нет
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
""" + COUNTERFACTUALS_DDL.format(name="counterfactuals")  # один DDL на схему и пересоздание


WRITE_LOCK = threading.RLock()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Многошаговая запись под процесс-wide замком.

    Одно соединение делят 4 потока (runner, кнопки, монитор, API): без замка
    commit() соседнего потока фиксирует половину чужой последовательности
    (sqlite3.threadsafety=3 спасает от падений, но не от перемешанных
    транзакций). Исключение внутри -> rollback, наружу пробрасывается.
    """
    with WRITE_LOCK:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def connect(db_path: Path) -> sqlite3.Connection:
    """Открыть журнал, применить схему, проставить версию.

    check_same_thread=False: соединение общее для потоков демона; одиночные
    вызовы сериализует сам sqlite3, многошаговые записи — transaction().
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
    existing = int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"])
    if existing < SCHEMA_VERSION and all(v in MIGRATIONS for v in range(existing + 1, SCHEMA_VERSION + 1)):
        try:
            _migrate(conn, existing)
        except Exception:
            conn.close()  # 2026-10-16: транзакция откачена, журнал на старой версии — не держим файл
            raise
        existing = SCHEMA_VERSION
    if existing != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"journal schema v{existing} != code v{SCHEMA_VERSION}; "
            "run archive + migration first"
        )
    return conn


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """Поэтапно до SCHEMA_VERSION; ADD COLUMN идемпотентен (колонка уже есть — пропуск).

    Всё — одной транзакцией (2026-10-16): sqlite3 не открывает ее сам перед DDL, и
    ALTER/CREATE/DROP раньше коммитились по одному — сбой посреди пересоздания
    таблицы оставлял бы журнал между версиями. Явный BEGIN: всё или ничего."""
    with transaction(conn):
        if not conn.in_transaction:
            conn.execute("BEGIN")
        for version in range(from_version + 1, SCHEMA_VERSION + 1):
            for table, column, decl in MIGRATIONS[version]:
                cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            for table in REBUILDS.get(version, []):
                _rebuild_table(conn, table)
            conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'", (str(version),)
            )


def _rebuild_table(conn: sqlite3.Connection, table: str) -> None:
    """Пересоздание таблицы по TABLE_DDL с переносом данных (рецепт SQLite «12 шагов»).

    Новая таблица -> INSERT ... SELECT общих колонок (id сохраняются) -> DROP старой ->
    RENAME -> индексы старой таблицы заново; счетчик AUTOINCREMENT не откатывается.
    UNIQUE/CHECK — из DDL. Вызывается внутри транзакции _migrate."""
    tmp = f"{table}__v{SCHEMA_VERSION}"
    old_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    indexes = [
        r["sql"] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table,),
        )
    ]
    seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
    conn.execute(f"DROP TABLE IF EXISTS {tmp}")
    conn.execute(TABLE_DDL[table].format(name=tmp))
    new_cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({tmp})")}
    cols = ", ".join(c for c in old_cols if c in new_cols)
    conn.execute(f"INSERT INTO {tmp} ({cols}) SELECT {cols} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
    for sql in indexes:
        conn.execute(sql)
    if seq is not None:
        cur = conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name=?", (seq["seq"], table)
        )
        if cur.rowcount == 0:  # таблица была пуста: строки счетчика у новой еще нет
            conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (table, seq["seq"]))
