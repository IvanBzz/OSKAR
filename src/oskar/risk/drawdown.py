"""Стоп по просадке (settings.safe_mode.drawdown_pct) — защелка капитала.

Блок H, 2026-10-16: ключ drawdown_pct был ложной ручкой («НЕ РЕАЛИЗОВАН»). Теперь:
- пик equity — в meta журнала (equity_peak); его ведет монитор позиций на полном тике
  (60 с) по equity, запрошенному у биржи на этом тике (LiveEquity ниже). Считается ТОЛЬКО
  equity с биржи: дефолтные $1000 до первого ответа биржи дали бы ложную «просадку»;
- equity <= пик * (1 - drawdown_pct/100) -> защелка drawdown_halt в meta + алерт;
- защелка персистентна (переживает рестарт) и сама НЕ снимается (урок v1): исполнитель
  отказывает в новых входах, монитор сопровождает и защищает открытые позиции;
- снятие — только `oskar drawdown-reset` (POST /drawdown/reset): пик := текущий equity.

Ревью Codex R1, 2026-10-16: runner обновляет equity раз в час, и минутная проверка видела
устаревшее значение с источником exchange — баланс 1000 -> 890 не взводил защелку почти час.
Теперь LiveEquity: монитор на полном тике сам спрашивает биржу (тот же snapshot_balance, что
у runner'а, с таймаутом), исполнитель перед входом берет equity не старше EQUITY_MAX_AGE_SEC
(иначе свежий запрос) и по нему же проверяет защелку — до исполнения.
"""

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from oskar.journal.db import transaction

logger = logging.getLogger("oskar.drawdown")

PEAK_KEY = "equity_peak"
HALT_KEY = "drawdown_halt"
DEFAULT_DRAWDOWN_PCT = 10.0
EQUITY_MAX_AGE_SEC = 120.0  # вход — только по equity с биржи не старше этого
EQUITY_FETCH_TIMEOUT_SEC = 15.0  # запрос баланса дольше — сбой: тик монитора не висит


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


class LiveEquity:
    """Equity с биржи и момент ответа — общий для монитора (защелка на полном тике),
    исполнителя (свежесть перед входом) и runner'а (сайзинг). Запросы последовательны:
    один ccxt-экземпляр на три потока. Таймаут — поток-исполнитель запроса с join: зависший
    запрос не держит тик, а следующий не плодит потоки, пока висит прежний."""

    def __init__(self, fetch_fn, *, timeout_sec: float = EQUITY_FETCH_TIMEOUT_SEC, clock=time.monotonic):
        self._fetch = fetch_fn  # () -> float | None: snapshot_balance(view)["total"]; None — молчит
        self._timeout = timeout_sec
        self._clock = clock
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self.value: float | None = None  # последний ответ биржи
        self.at: float | None = None  # clock() этого ответа

    def age(self) -> float | None:
        return None if self.at is None else self._clock() - self.at

    def refresh(self) -> float | None:
        """Запрос к бирже сейчас. Сбой, таймаут, не-положительное — None и строка в лог."""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                logger.warning("equity fetch skipped: previous request still hanging")
                return None
            box: dict = {}

            def run():
                try:
                    box["value"] = self._fetch()
                except Exception as e:  # noqa: BLE001 - в лог ниже, тик не роняем
                    box["error"] = e

            worker = threading.Thread(target=run, name="oskar-equity", daemon=True)
            self._worker = worker
            worker.start()
            worker.join(self._timeout)
            if worker.is_alive():
                logger.warning("equity fetch timed out after %.0fs", self._timeout)
                return None
            if "error" in box:
                logger.warning("equity fetch failed: %s: %s", type(box["error"]).__name__, box["error"])
                return None
            try:
                value = float(box.get("value"))
            except (TypeError, ValueError):
                value = 0.0
            if not value > 0:
                logger.warning("equity fetch: no exchange value (%r)", box.get("value"))
                return None
            self.value, self.at = value, self._clock()
            return value

    def fresh(self, max_age_sec: float = EQUITY_MAX_AGE_SEC) -> float | None:
        """Equity не старше max_age_sec; устарел — свежий запрос; не ответила — None."""
        age = self.age()
        if age is not None and 0 <= age <= max_age_sec:
            return self.value
        return self.refresh()


class DrawdownGuard:
    """Пик equity и защелка просадки. check() зовет монитор на полном тике."""

    def __init__(
        self,
        journal: sqlite3.Connection,
        drawdown_pct: float,
        *,
        equity_fn=None,  # () -> float: тот же equity, что у исполнителя (runner.equity_usd)
        equity_source_fn=lambda: "exchange",  # () -> exchange | persisted | default
        live: LiveEquity | None = None,  # есть — check() спрашивает биржу сам (ревью Codex R1)
    ):
        pct = float(drawdown_pct)
        if not 0.0 < pct < 100.0:
            raise ValueError(f"drawdown_pct must be in (0, 100), got {drawdown_pct!r}")
        if equity_fn is None and live is None:
            raise ValueError("DrawdownGuard needs equity_fn or live")
        self._j = journal
        self.drawdown_pct = pct
        self._equity = equity_fn
        self._source = equity_source_fn
        self._live = live

    def live_equity(self) -> float | None:
        """Equity с биржи; иначе None (persisted/default — не основание ни для пика, ни для стопа).
        С LiveEquity — свежий запрос к бирже, а не часовой кеш runner'а."""
        if self._live is not None:
            return self._live.refresh()
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
        return self.observe(equity)

    def observe(self, equity: float) -> DrawdownHalt | None:
        """Пик и защелка по equity С БИРЖИ (монитор — свой запрос, исполнитель — свежий перед
        входом). Возвращает защелку, только если она взведена этим вызовом."""
        if not equity > 0:
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
        logger.error("drawdown halt engaged: %s", halt.message())
        return halt

    def reset(self) -> dict:
        """Снятие оператором: только по equity с биржи (сброс пика на выдуманные $1000 — нет)."""
        equity = self.live_equity()
        if equity is None:
            source = "нет ответа" if self._live is not None else self._source()
            raise DrawdownError(
                f"equity с биржи недоступен (источник: {source}) — защелка и пик не тронуты"
            )
        return reset_drawdown(self._j, equity)
