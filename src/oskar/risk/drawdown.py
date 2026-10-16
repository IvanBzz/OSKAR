"""Стоп по просадке (settings.safe_mode.drawdown_pct) — защелка капитала.

Блок H, 2026-10-16: ключ drawdown_pct был ложной ручкой («НЕ РЕАЛИЗОВАН»). Теперь:
- пик equity — в meta журнала (equity_peak); его ведет монитор позиций на полном тике
  (60 с) по тому же equity, что видит исполнитель (runner.equity_usd). Считается ТОЛЬКО
  equity с биржи: дефолтные $1000 до первого ответа биржи дали бы ложную «просадку»;
- equity <= пик * (1 - drawdown_pct/100) -> защелка drawdown_halt в meta + алерт;
- защелка персистентна (переживает рестарт) и сама НЕ снимается (урок v1): исполнитель
  отказывает в новых входах, монитор сопровождает и защищает открытые позиции;
- снятие — только `oskar drawdown-reset` (POST /drawdown/reset): пик := текущий equity.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from oskar.journal.db import transaction

PEAK_KEY = "equity_peak"
HALT_KEY = "drawdown_halt"
DEFAULT_DRAWDOWN_PCT = 10.0


class DrawdownError(RuntimeError):
    pass


@dataclass(frozen=True)
class DrawdownHalt:
    equity: float  # equity в момент срабатывания
    peak: float
    at: str | None = None

    @property
    def pct(self) -> float:
        return (1.0 - self.equity / self.peak) * 100.0 if self.peak > 0 else 0.0

    def message(self) -> str:
        return (f"стоп по просадке: equity {self.equity:.2f} от пика {self.peak:.2f} "
                f"(−{self.pct:.1f}%) — новые входы заблокированы до ручного снятия")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _meta(journal: sqlite3.Connection, key: str) -> str | None:
    row = journal.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def read_peak(journal: sqlite3.Connection) -> float | None:
    raw = _meta(journal, PEAK_KEY)
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None  # битое значение: пик переустановится по текущему equity


def read_halt(journal: sqlite3.Connection) -> DrawdownHalt | None:
    """Защелка, если взведена. Битая запись — все равно защелка (fail-closed)."""
    raw = _meta(journal, HALT_KEY)
    if raw is None:
        return None
    try:
        d = json.loads(raw)
        return DrawdownHalt(float(d["equity"]), float(d["peak"]), d.get("at"))
    except (ValueError, KeyError, TypeError):
        return DrawdownHalt(0.0, 0.0, None)


def drawdown_status(journal: sqlite3.Connection) -> dict:
    """Для GET /status: пик и состояние защелки."""
    halt = read_halt(journal)
    return {
        "peak": read_peak(journal),
        "halt": None if halt is None else {
            "equity": halt.equity, "peak": halt.peak, "pct": round(halt.pct, 2), "at": halt.at,
        },
    }


def reset_drawdown(journal: sqlite3.Connection, equity: float) -> dict:
    """Ручное снятие: защелка удаляется, пик := текущий equity (одной транзакцией)."""
    if not equity > 0:
        raise DrawdownError(f"equity {equity!r} — пик не сброшен")
    with transaction(journal):
        halt = read_halt(journal)
        peak_before = read_peak(journal)
        journal.execute("DELETE FROM meta WHERE key = ?", (HALT_KEY,))
        journal.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (PEAK_KEY, repr(equity)))
    return {
        "drawdown_halt": False, "was_halted": halt is not None,
        "equity": equity, "peak_before": peak_before, "peak": equity,
    }


class DrawdownGuard:
    """Пик equity и защелка просадки. check() зовет монитор на полном тике."""

    def __init__(
        self,
        journal: sqlite3.Connection,
        drawdown_pct: float,
        *,
        equity_fn,  # () -> float: тот же equity, что у исполнителя (runner.equity_usd)
        equity_source_fn=lambda: "exchange",  # () -> exchange | persisted | default
    ):
        pct = float(drawdown_pct)
        if not 0.0 < pct < 100.0:
            raise ValueError(f"drawdown_pct must be in (0, 100), got {drawdown_pct!r}")
        self._j = journal
        self.drawdown_pct = pct
        self._equity = equity_fn
        self._source = equity_source_fn

    def live_equity(self) -> float | None:
        """Equity с биржи; иначе None (persisted/default — не основание ни для пика, ни для стопа)."""
        source = self._source()
        if source != "exchange":
            return None
        equity = float(self._equity())
        return equity if equity > 0 else None

    def check(self) -> DrawdownHalt | None:
        """Возвращает защелку, ТОЛЬКО если она взведена этим вызовом (алерт — один раз).
        Взведенная защелка не трогается — ни пик, ни она сама, до ручного снятия."""
        equity = self.live_equity()
        if equity is None:
            return None
        with transaction(self._j):
            if read_halt(self._j) is not None:
                return None
            peak = read_peak(self._j)
            if peak is None or equity > peak:
                self._j.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (PEAK_KEY, repr(equity))
                )
                return None
            if equity > peak * (1.0 - self.drawdown_pct / 100.0):
                return None
            halt = DrawdownHalt(equity, peak, _now_iso())
            self._j.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (HALT_KEY, json.dumps({"equity": equity, "peak": peak, "at": halt.at})),
            )
        return halt

    def reset(self) -> dict:
        """Снятие оператором: только по equity с биржи (сброс пика на выдуманные $1000 — нет)."""
        equity = self.live_equity()
        if equity is None:
            raise DrawdownError(
                f"equity с биржи недоступен (источник: {self._source()}) — защелка и пик не тронуты"
            )
        return reset_drawdown(self._j, equity)
