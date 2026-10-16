"""Чей ордер OSKAR на бирже: run_id из clientOrderId против журнала этой машины.

Блок I, 2026-10-16: межмашинного замка нет (port-lock и data/oskard.lock — только на своей
машине), а журнал у каждой машины свой (data/ не в git). Второй oskard, по ошибке запущенный
на ноутбуке, видел ордера первого как «сирот» (cid OSKAR-, сделки в его журнале нет) и снимал
их SL/TP — урок v1 с двойной ADA, только хуже: позиция без стопа.

cid = OSKAR-{run_id}-{sha} (executor.make_client_order_id). «Свой» запуск — run_id, который
знает журнал: meta run_started:<id> (каждый старт демона), trades.run_id, decisions.run_id,
текущий запуск. Чужой run_id — ордер другой машины (или журнала, архивированного при смене
схемы): его не трогаем, а останавливаем новые входы (SAFE MODE) и зовем оператора.
"""

import sqlite3
from datetime import UTC, datetime

RUN_META_PREFIX = "run_started:"


def cid_run_id(client_order_id: str) -> str | None:
    """'OSKAR-ab12cd34-<sha>[-sl-r1]' -> 'ab12cd34' (нижний регистр); не наш формат -> None."""
    parts = (client_order_id or "").lower().split("-")
    if len(parts) < 3 or parts[0] != "oskar" or not parts[1] or not parts[2]:
        return None
    return parts[1]


def register_run(journal: sqlite3.Connection, run_id: str) -> None:
    """Старт демона: запуск известен журналу еще до первой сделки и первого решения —
    вход, упавший между отправкой и записью, останется «своим» сиротой."""
    from oskar.journal.db import transaction

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    with transaction(journal):
        journal.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            (RUN_META_PREFIX + run_id.lower(), now),
        )


def run_known(journal: sqlite3.Connection, run_id: str, current_run_id: str | None = None) -> bool:
    """Знает ли журнал этот запуск. Сбой чтения журнала — исключение наверх (вызывающий
    считает ордер чужим: не снимать надежнее, чем снять стоп другой машины)."""
    rid = run_id.lower()
    if current_run_id is not None and rid == current_run_id.lower():
        return True
    row = journal.execute(
        "SELECT EXISTS(SELECT 1 FROM meta WHERE key = ?) "
        "OR EXISTS(SELECT 1 FROM trades WHERE lower(run_id) = ?) "
        "OR EXISTS(SELECT 1 FROM decisions WHERE lower(run_id) = ?)",
        (RUN_META_PREFIX + rid, rid, rid),
    ).fetchone()
    return bool(row[0])
