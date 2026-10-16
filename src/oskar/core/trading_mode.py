"""Режим исполнения: «с подтверждением» (карточка с кнопками) или «автомат»
(сделка ставится сама, карточка приходит уведомлением).

Пожелание Ивана 2026-10-16: «пока что запускаем автоматический режим; в боте —
переключатель как ползунком: автоматический / с подтверждением». Это шаг
«подтверждение -> автономия» из стартовых решений.

Ограничения:
- автомат — ТОЛЬКО paper/VST (settings.mode=paper и exchange.sandbox=true); реал закрыт
  гейтом Фазы 5 (PF >= 1.3 на 60 сделках), включить автомат там нельзя;
- проверки те же, что у кнопки: лестница при создании плана, equity с биржи, лимит
  позиций и дубль, дрейф цены, pause/safe_mode (approve в них не проходит);
- предохранитель: BREAKER_FAILURES отказов биржи подряд -> назад «с подтверждением»
  + алерт (сломанный ключ/биржа не должны молча жечь карточки). Блок H, 2026-10-16:
  считает по машинному коду исхода (ExecutionOutcome.kind), а не по подстроке; «вход
  есть, защита не встала» — сбой, размыкает сразу (цикл «открыл -> стоп не встал ->
  закрыл» раньше считался успехом); отказы предпроверок предохранитель не трогают.
"""

import html
import logging
import sqlite3
import threading
from dataclasses import dataclass

from oskar.core.approvals import resolve_approval
from oskar.core.executor import (
    KIND_ENTRY_UNKNOWN,
    KIND_ERROR,
    KIND_EXCHANGE_REJECTED,
    KIND_PROTECTION_FAILED,
)
from oskar.journal.db import transaction

logger = logging.getLogger("oskar.mode")

MODE_META_KEY = "execution_mode"
AUTO = "auto"
CONFIRM = "confirm"
MODES = (CONFIRM, AUTO)
MODE_LABELS = {AUTO: "автомат", CONFIRM: "с подтверждением"}
BREAKER_FAILURES = 3
# Исходы, которые считает предохранитель; refused_precheck (цена ушла, лимит, R:R по факту,
# флаги защиты) — нормальная работа, не сбой биржи. Таймаут входа — тоже сбой биржи.
BREAKER_KINDS = {KIND_EXCHANGE_REJECTED, KIND_PROTECTION_FAILED, KIND_ENTRY_UNKNOWN, KIND_ERROR}
BREAKER_TRIP_AT_ONCE = {KIND_PROTECTION_FAILED}  # позиция без стопа — с первого раза


class TradingMode:
    """Текущий режим. Хранится в meta журнала — переживает рестарт; без записи — default."""

    def __init__(self, journal: sqlite3.Connection, *, allowed: bool, default: str = CONFIRM):
        self._j = journal
        self.allowed = allowed  # автомат разрешен средой (paper + sandbox)
        self._default = default if default in MODES else CONFIRM

    def get(self) -> str:
        if not self.allowed:
            return CONFIRM
        row = self._j.execute("SELECT value FROM meta WHERE key = ?", (MODE_META_KEY,)).fetchone()
        value = row[0] if row else self._default
        return value if value in MODES else CONFIRM

    def is_auto(self) -> bool:
        return self.get() == AUTO

    def set(self, mode: str) -> str:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        if mode == AUTO and not self.allowed:
            raise ValueError("автомат разрешен только на paper/VST (реал закрыт гейтом Фазы 5)")
        with transaction(self._j):
            self._j.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (MODE_META_KEY, mode)
            )
        logger.warning("execution mode -> %s", mode)
        return mode


@dataclass(frozen=True)
class AutoResult:
    executed: bool  # вход ушел на биржу (сделка в журнале) — кнопки не нужны
    note: str  # одна строка для карточки


class AutoExecutor:
    """Исполнение без нажатия: approve -> тот же охраняемый исполнитель, что у кнопок."""

    def __init__(self, mode: TradingMode, state, execute_fn, notify=None):
        self.mode = mode
        self._state = state
        self._execute = execute_fn  # (decision_id, entry_type) -> ExecutionOutcome (equity_guarded)
        self._notify = notify
        self._failures = 0
        self._lock = threading.Lock()

    def run(self, decision_id: str, entry_type: str) -> AutoResult:
        j = self._state.journal
        out = resolve_approval(
            j, decision_id, "approve", paused=self._state.paused, safe_mode=self._state.safe_mode
        )
        if not out.ok:
            return AutoResult(False, f"🤖 автомат не исполнил: {out.message}")
        try:
            res = self._execute(decision_id, entry_type)
        except Exception as e:  # noqa: BLE001 - автомат не роняет цикл
            logger.error("auto execution crashed for %s: %s", decision_id, e)
            res = None
            message, kind = f"ошибка исполнения: {e}", KIND_ERROR
        else:
            message, kind = res.message, res.kind
        if res is not None and res.ok:
            with self._lock:
                self._failures = 0
            return AutoResult(True, f"🤖 автомат: {message}")
        if res is not None and res.entry_sent:
            # вход ушел (сделка в журнале): approve не откатываем, кнопки не нужны — но это сбой
            self._count_failure(kind, message)
            return AutoResult(True, f"🤖 автомат: {message}")
        # до биржи не дошло: approve откатываем — карточка уйдет с кнопками для ручного решения
        with transaction(j):
            j.execute(
                "UPDATE pending_approvals SET resolution=NULL, resolved_at=NULL WHERE decision_id=?",
                (decision_id,),
            )
        self._count_failure(kind, message)
        return AutoResult(False, f"🤖 автомат не исполнил: {message}")

    def _count_failure(self, kind: str, message: str) -> None:
        if kind not in BREAKER_KINDS:
            return  # отказ предпроверок (дубль, лимит, дрейф, R:R) — нормальная работа
        with self._lock:
            self._failures += 1
            failures = self._failures
            tripped = kind in BREAKER_TRIP_AT_ONCE or failures >= BREAKER_FAILURES
            if tripped:
                self._failures = 0
        if tripped:
            try:
                self.mode.set(CONFIRM)
            except Exception as e:  # noqa: BLE001
                logger.error("breaker could not switch mode: %s", e)
            why = (f"защита не встала ({message[:200]})" if kind in BREAKER_TRIP_AT_ONCE
                   else f"{failures} сбоя биржи подряд")
            # send_text шлет parse_mode=HTML, а в тексте бывает ошибка биржи
            text = html.escape(f"🛑 Автомат выключен: {why}. Режим — «с подтверждением», "
                               "включить снова — /mode", quote=False)
            logger.error(text)
            if self._notify is not None:
                try:
                    self._notify(text)
                except Exception as e:  # noqa: BLE001
                    logger.warning("notify failed: %s", e)
