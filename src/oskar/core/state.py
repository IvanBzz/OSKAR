"""Рантайм-состояние демона — единый объект, разделяемый API и циклом."""

import sqlite3
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class RuntimeState:
    journal: sqlite3.Connection
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    started_at: float = field(default_factory=time.time)
    paused: bool = False
    safe_mode: bool = False
    panic_at: float | None = None

    @property
    def uptime_sec(self) -> int:
        return int(time.time() - self.started_at)

    def panic(self) -> None:
        """Ст. 6: немедленно и безусловно. Флаги ставятся ДО любых действий."""
        self.safe_mode = True
        self.paused = True
        self.panic_at = time.time()
