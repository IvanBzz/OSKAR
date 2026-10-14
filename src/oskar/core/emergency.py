"""Emergency Stop (ст. 6) — одна реализация для всех поверхностей panic.

Ревью 2026-10-13: ст. 6 обещает panic из Telegram `/panic`, CLI `oskar panic`
и API, а существовал только API — логика жила внутри эндпоинта. Теперь API,
поллер сигнального бота и CLI (через API) зовут одни и те же функции, порядок
не меняется:

1. флаги в памяти (`state.panic()`) — ПЕРВЫМИ, до любого ввода-вывода: ничто
   не смеет задержать panic (ст. 6 п. 2);
2. флаг в `meta` — panic переживает рестарт (ст. 6 п. 3: снятие только явной
   командой оператора);
3. биржа: `monitor.panic_flatten()` — снять входы, закрыть позиции.
"""

import threading
from datetime import UTC, datetime

from oskar.core.state import RuntimeState
from oskar.journal.db import transaction

PANIC_META_KEY = "panic_engaged"

# Два канала сразу (Telegram + API/CLI) не должны закрывать одни и те же позиции
# параллельно: второй ждет первого и проходит по уже пустым спискам. Флаги
# второго вызова ставятся ДО замка — ожидание касается только биржевой части.
_FLATTEN_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def engage_panic(state: RuntimeState, monitor=None) -> dict:
    """Ст. 6 п. 1: немедленно и безусловно. Ответ — тот же JSON, что отдавал `/panic`:
    exchange=None — монитора нет (нет ключей биржи), биржа не тронута."""
    state.panic()  # флаги ПЕРВЫМИ, ничто не смеет задержать
    result = {"panic": True, "safe_mode": True, "paused": True, "exchange": None}
    try:  # флаг переживает рестарт — восстановление только явной командой
        with transaction(state.journal):
            state.journal.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (PANIC_META_KEY, _now_iso())
            )
    except Exception as e:  # noqa: BLE001 - флаги уже стоят, биржу снимаем все равно
        result["persist_error"] = str(e)
    if monitor is not None:
        with _FLATTEN_LOCK:
            try:
                result["exchange"] = monitor.panic_flatten()  # отмена ордеров + закрытие позиций
            except Exception as e:  # noqa: BLE001 - биржа недоступна: оператор видит это в ответе
                result["exchange"] = {"error": str(e)}
    return result


def reset_panic(state: RuntimeState) -> dict:
    """Ст. 6 п. 3: только явной командой оператора. Снимает panic и safe_mode,
    пауза остается — resume отдельным решением.

    Сначала журнал, потом память: не записалось — panic остается взведенным
    целиком (раньше флаги снимались до записи, и сбой БД давал «panic снят до
    рестарта, а после рестарта снова взведен»)."""
    with transaction(state.journal):
        state.journal.execute("DELETE FROM meta WHERE key = ?", (PANIC_META_KEY,))
    state.panic_at = None
    state.safe_mode = False
    return {"panic": False, "safe_mode": False, "paused": state.paused}


def panic_engaged(journal) -> str | None:
    """Момент последнего panic, если он не был сброшен оператором (переживает рестарт)."""
    row = journal.execute("SELECT value FROM meta WHERE key = ?", (PANIC_META_KEY,)).fetchone()
    return row[0] if row else None
