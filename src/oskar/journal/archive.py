"""Архив журнала перед большим прогоном (политика v1: «старые числа тянут на дно»).

Снепшот data/oskar.db -> data/archive/oskar_<UTC timestamp>.db перед прогоном
с новой конфигурацией: статистика провального прогона не должна отравлять
контекст следующего (v1: PF 0.12 в живой БД делал performance-секцию каждого
промпта пессимистичной).

Триггеры (v1 vault, db-archive-policy): смена конфига, крупное изменение
промптов, фиксы после аудита, пауза > 7 дней.
"""

import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def archive_journal(db_path: Path, archive_dir: Path | None = None) -> Path:
    """Снепшот журнала. Возвращает путь архива. WAL корректно чекпоинтится."""
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    archive_dir = archive_dir or db_path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    target = archive_dir / f"{db_path.stem}_{stamp}{db_path.suffix}"
    shutil.copy2(db_path, target)
    return target
