"""Рантайм-состояние демона — единый объект, разделяемый API и циклом."""

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RuntimeState:
    journal: sqlite3.Connection
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    started_at: float = field(default_factory=time.time)
    paused: bool = False
    safe_mode: bool = False
    panic_at: float | None = None
    # SAFE MODE на двух носителях (2026-10-16, ревью Codex R1): запись в meta упала (SQLite
    # занят) — флаг стоял только в памяти, и рестарт супервизором молча снимал защиту.
    safe_mode_unsaved: bool = False  # meta не записана: монитор и runner повторяют каждый тик
    safe_mode_record: str | None = None  # JSON {"reason","at"} для повтора записи
    safe_mode_flag: Path | None = None  # маркер-файл (демон: data/safe_mode.flag); None — без файла

    @property
    def uptime_sec(self) -> int:
        return int(time.time() - self.started_at)

    def panic(self) -> None:
        """Ст. 6: немедленно и безусловно. Флаги ставятся ДО любых действий."""
        self.safe_mode = True
        self.paused = True
        self.panic_at = time.time()
