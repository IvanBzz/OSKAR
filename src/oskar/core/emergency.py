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

SAFE MODE (блок H, 2026-10-16) тоже переживает рестарт: супервизор блока G перезапускает
демон, и флаг только в памяти («позиция без SL», расхождение с биржей) снимался бы молча.
Взвод — флаг в памяти и запись в `meta` (с причиной), старт — восстановление с алертом,
снятие — только явное (`reset_panic`: CLI `oskar panic-reset` / POST /panic/reset).
"""

import html
import json
import logging
import threading
from datetime import UTC, datetime

from oskar.core.state import RuntimeState
from oskar.journal.db import transaction

logger = logging.getLogger("oskar.emergency")

PANIC_META_KEY = "panic_engaged"
SAFE_MODE_META_KEY = "safe_mode_engaged"

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
        # SAFE MODE снимается здесь же и тоже сначала в журнале (блок H, 2026-10-16)
        state.journal.execute("DELETE FROM meta WHERE key = ?", (SAFE_MODE_META_KEY,))
    state.panic_at = None
    state.safe_mode = False
    return {"panic": False, "safe_mode": False, "paused": state.paused}


def panic_engaged(journal) -> str | None:
    """Момент последнего panic, если он не был сброшен оператором (переживает рестарт)."""
    row = journal.execute("SELECT value FROM meta WHERE key = ?", (PANIC_META_KEY,)).fetchone()
    return row[0] if row else None


def engage_safe_mode(state: RuntimeState, reason: str) -> bool:
    """Взвести SAFE MODE: флаг в памяти ПЕРВЫМ, затем запись в meta с причиной (переживает
    рестарт). Запись не удалась — флаг стоит до рестарта, ошибка в лог. True — взведен
    этим вызовом (алерт шлет вызывающий, один раз)."""
    if state.safe_mode:
        return False
    state.safe_mode = True
    try:
        with transaction(state.journal):
            state.journal.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (SAFE_MODE_META_KEY, json.dumps({"reason": reason, "at": _now_iso()}, ensure_ascii=False)),
            )
    except Exception as e:  # noqa: BLE001 - флаг уже стоит; рестарт его не удержит — громко в лог
        logger.error("SAFE MODE not persisted (a restart would lift it): %s", e)
    return True


def safe_mode_engaged(journal) -> dict | None:
    """{"reason", "at"} взведенного и не снятого SAFE MODE (переживает рестарт)."""
    row = journal.execute("SELECT value FROM meta WHERE key = ?", (SAFE_MODE_META_KEY,)).fetchone()
    if row is None:
        return None
    try:
        info = json.loads(row[0])
        return {"reason": str(info.get("reason", "")), "at": info.get("at")}
    except (ValueError, AttributeError):
        return {"reason": str(row[0]), "at": None}  # битая запись — все равно SAFE MODE


def restore_safe_mode(state: RuntimeState, notify=None) -> str | None:
    """Старт демона: SAFE MODE из meta -> флаг + алерт. Возвращает причину или None."""
    info = safe_mode_engaged(state.journal)
    if info is None:
        return None
    state.safe_mode = True
    reason = info["reason"] or "причина не записана"
    if notify is not None:
        try:  # send_text шлет parse_mode=HTML, а в причине бывает текст ошибки биржи
            notify("🛑 " + html.escape(f"SAFE MODE восстановлен после рестарта: {reason}", quote=False))
        except Exception as e:  # noqa: BLE001 - канал не влияет на флаг
            logger.warning("notify failed: %s", e)
    return reason
