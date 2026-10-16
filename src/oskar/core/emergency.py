"""Emergency Stop (ст. 6) — одна реализация для всех поверхностей panic.

Ревью 2026-10-13: ст. 6 обещает panic из Telegram `/panic`, CLI `oskar panic`
и API, а существовал только API — логика жила внутри эндпоинта. Теперь API,
поллер сигнального бота и CLI (через API) зовут одни и те же функции, порядок
не меняется:

1. флаги в памяти (`state.panic()`) — ПЕРВЫМИ, до любого ввода-вывода: ничто
   не смеет задержать panic (ст. 6 п. 2);
2. флаг в `meta` — panic переживает рестарт (ст. 6 п. 3: снятие только явной
   командой оператора);
3. биржа: `monitor.panic_flatten()` — снять входы, закрыть позиции;
4. входы «в процессе» (2026-10-16, ревью Codex R1): исполнитель мог пройти последнюю
   проверку флагов до panic и уже слать вход — записи в журнале еще нет, flatten ее не
   видит. Ждем до PANIC_INFLIGHT_WAIT_SEC, пока реестр in-flight опустеет, и делаем
   финальный проход по только что записанным сделкам; не опустел — проверка этих входов
   по clientOrderId (ниже, 3-й круг H3).

SAFE MODE (блок H, 2026-10-16) тоже переживает рестарт: супервизор блока G перезапускает
демон, и флаг только в памяти («позиция без SL», расхождение с биржей) снимался бы молча.
Взвод — флаг в памяти и запись в `meta` (с причиной), старт — восстановление с алертом,
снятие — только явное (`reset_panic`: CLI `oskar panic-reset` / POST /panic/reset).

Два носителя SAFE MODE (2026-10-16, ревью Codex R1): запись в meta падала (SQLite занят) —
флаг стоял только в памяти, повтора не было, и рестарт снимал защиту. Теперь второй
носитель — маркер-файл data/safe_mode.flag (атомарно: tmp + rename); несохраненная meta
повторяется на каждом тике монитора и runner'а; старт восстанавливает SAFE MODE по ЛЮБОМУ
носителю; panic-reset снимает оба.

Ревью Codex H2/I, 2026-10-16:
- ENTRY_GATE_LOCK — короткая критическая секция без сети: взвод panic/SAFE MODE в памяти и
  допуск входа к отправке (executor._admit) не перемежаются. Вход, не допущенный к моменту
  взвода, на биржу уже не уйдет — panic ждет только допущенные (раньше ждал и вход, висящий
  на сетевом плече);
- финальный проход flatten — всегда: вход мог записаться и выйти из реестра между чтением
  списка первым проходом и ожиданием (раньше проход шел, только если ждали);
- Telegram — после биржевых действий и вне _FLATTEN_LOCK, одним сообщением: недоступный
  Telegram (2 таймаута по 15 с на сообщение) держал замок и откладывал закрытие позиций.

Ревью Codex H3, 3-й круг (2026-10-16) — упрощение: по symbol/side panic ничего не закрывает
«на всякий случай» (слежение chase закрывало ручную позицию той же пары и терялось при
рестарте). Вход, не завершенный за окно, и pending без id биржи проверяются по clientOrderId /
id ордера: исполнение доказано — закрыть ровно его qty и снять его защиту по id; не доказано —
запись в список неясных входов (meta PANIC_UNCLEAR_KEY: переживает рестарт), SAFE MODE на обоих
носителях и один алерт. Монитор проверяет неясные входы по id на каждом тике (core/position_monitor).
panic-reset снимает список вместе с флагами: решение оператора.
"""

import contextlib
import html
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from oskar.core.state import RuntimeState
from oskar.journal.db import WRITE_LOCK, transaction

logger = logging.getLogger("oskar.emergency")

PANIC_META_KEY = "panic_engaged"
# Входы с неясным исходом при panic: {база cid: запись} JSON — переживают рестарт (ревью H3, 3-й круг)
PANIC_UNCLEAR_KEY = "panic_unclear_entries"
SAFE_MODE_META_KEY = "safe_mode_engaged"
SAFE_MODE_FLAG_NAME = "safe_mode.flag"  # в data/ демона: второй носитель SAFE MODE
PANIC_INFLIGHT_WAIT_SEC = 15.0  # panic ждет входы «в процессе» до финального прохода
PANIC_INFLIGHT_POLL_SEC = 0.25

# Два канала сразу (Telegram + API/CLI) не должны закрывать одни и те же позиции
# параллельно: второй ждет первого и проходит по уже пустым спискам. Флаги
# второго вызова ставятся ДО замка — ожидание касается только биржевой части.
_FLATTEN_LOCK = threading.Lock()
# Запись носителей SAFE MODE: взвод, повтор с тиков и panic-reset не перемешиваются
# (повтор после reset воскресил бы SAFE MODE в meta).
_SAFE_MODE_LOCK = threading.Lock()
# Взвод panic/SAFE MODE в памяти и допуск входа к отправке (ревью Codex H2/I, 2026-10-16).
# Под ним — только память: ни сети, ни журнала; держится микросекунды.
ENTRY_GATE_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ------------------------------------------------------------------- panic


def _flatten_pass(fn) -> dict:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - биржа недоступна: оператор видит это в ответе
        return {"error": str(e)}


def _merge_passes(*parts: dict) -> dict:
    if parts and all("error" in part for part in parts):
        return dict(parts[0])  # биржа недоступна целиком — ответ прежней формы {"error": ...}
    out = {"cancelled": 0, "closed": 0, "errors": []}
    unclear: set[str] = set()  # входы с неясным исходом: ничего не закрыто, проверка по id
    for part in parts:
        if "error" in part:
            out["errors"].append(str(part["error"]))
            continue
        out["cancelled"] += int(part.get("cancelled", 0))
        out["closed"] += int(part.get("closed", 0))
        out["errors"].extend(part.get("errors") or [])
        unclear.update(part.get("unclear") or [])
    if unclear:
        out["unclear"] = sorted(unclear)
    return out


def _await_inflight(wait_sec: float, sleep, clock) -> dict:
    """Входы, допущенные к отправке и не завершенные за окно: {ключ cid: (symbol, side)}.
    Недопущенные не ждем: после взвода panic под ENTRY_GATE_LOCK их отклонит допуск."""
    from oskar.core.executor import admitted_entries  # executor импортирует этот модуль

    deadline = clock() + wait_sec
    while True:
        left = admitted_entries()
        if not left or clock() >= deadline:
            return left
        sleep(PANIC_INFLIGHT_POLL_SEC)


def _panic_summary(exchange: dict, left: dict) -> str:
    if "error" in exchange:
        return f"🚨 PANIC: биржа недоступна ({exchange['error']}) — проверь позиции вручную"
    text = (f"🚨 PANIC: снято входов {exchange.get('cancelled', 0)}, закрыто позиций "
            f"{exchange.get('closed', 0)}, ошибок {len(exchange.get('errors') or [])}")
    if left:
        text += f"; входов в процессе после окна: {len(left)} — проверены по clientOrderId"
    if exchange.get("unclear"):
        text += (f"; входов с неясным исходом: {len(exchange['unclear'])} — по symbol/side ничего "
                 "не закрыто, проверь позиции вручную (SAFE MODE)")
    return text


def engage_panic(state: RuntimeState, monitor=None, *, inflight_wait_sec: float = PANIC_INFLIGHT_WAIT_SEC,
                 sleep=time.sleep, clock=time.monotonic) -> dict:
    """Ст. 6 п. 1: немедленно и безусловно. Ответ — тот же JSON, что отдавал `/panic`:
    exchange=None — монитора нет (нет ключей биржи), биржа не тронута."""
    with ENTRY_GATE_LOCK:  # флаги ПЕРВЫМИ; замок — только против допуска входа, без сети
        state.panic()
    result = {"panic": True, "safe_mode": True, "paused": True, "exchange": None}
    try:  # флаг переживает рестарт — восстановление только явной командой
        with transaction(state.journal):
            state.journal.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (PANIC_META_KEY, _now_iso())
            )
    except Exception as e:  # noqa: BLE001 - флаги уже стоят, биржу снимаем все равно
        result["persist_error"] = str(e)
        # panic не записан: защиту держит SAFE MODE на двух носителях (маркер-файл сразу,
        # meta — повтором с тиков), иначе рестарт снял бы все (ревью Codex R1, 2026-10-16)
        with _SAFE_MODE_LOCK:
            state.safe_mode_record = _safe_mode_record(f"panic не записан в журнал: {e}")
            _persist_safe_mode(state)
    if monitor is None:
        return result
    # Алерты монитора этого потока — в буфер: Telegram после биржи и вне замка (ревью Codex H2/I)
    defer = getattr(monitor, "deferred_notes", None)
    with _FLATTEN_LOCK, (defer() if defer is not None else contextlib.nullcontext([])) as notes:
        begin = getattr(monitor, "panic_begin", None)
        if begin is not None:  # закрытие сделки — одна попытка за panic (неясный сбой не повторяем)
            begin()
        first = _flatten_pass(monitor.panic_flatten)  # отмена входов + закрытие позиций OSKAR
        # Вход, допущенный к отправке до panic: ждем его записи (ревью Codex R1, 2026-10-16)
        left = _await_inflight(inflight_wait_sec, sleep, clock)
        # Финальный проход — ВСЕГДА (ревью Codex H2/I, 2026-10-16): вход мог записаться и выйти
        # из реестра между чтением списка первым проходом и ожиданием — реестр уже пуст.
        parts = [first, _flatten_pass(monitor.panic_flatten)]
        if left:
            logger.critical("panic: %d entr(ies) still in flight after %.0fs: %s",
                            len(left), inflight_wait_sec, sorted(left))
            # По clientOrderId, не по symbol/side (ревью H3, 3-й круг): исполнение доказано —
            # закрыть его qty; нет — список неясных входов, SAFE MODE, алерт
            parts.append(_flatten_pass(lambda: monitor.panic_resolve_inflight(left)))
        result["exchange"] = _merge_passes(*parts)
        if left:
            result["exchange"]["inflight_unfinished"] = sorted(left)
    send = getattr(monitor, "send_notes", None)
    if send is not None:  # одним сообщением; сбой канала не роняет panic
        send([*notes, _panic_summary(result["exchange"], left)])
    return result


def reset_panic(state: RuntimeState) -> dict:
    """Ст. 6 п. 3: только явной командой оператора. Снимает panic и safe_mode,
    пауза остается — resume отдельным решением.

    Сначала журнал, потом память: не записалось — panic остается взведенным
    целиком (раньше флаги снимались до записи, и сбой БД давал «panic снят до
    рестарта, а после рестарта снова взведен»). Маркер-файл SAFE MODE удаляется до
    снятия флагов в памяти: не удалился — исключение, флаги стоят.

    Список неясных входов panic снимается здесь же (решение оператора; ревью H3, 3-й круг) и
    возвращается в ответе (unclear_dropped). WRITE_LOCK — на журнал И память: монитор добавляет
    в список только при взведенном panic, проверяя флаг под тем же замком, — снятый список не
    воскреснет."""
    with _SAFE_MODE_LOCK, WRITE_LOCK:
        with transaction(state.journal):
            dropped = sorted(unclear_entries(state.journal))
            state.journal.execute("DELETE FROM meta WHERE key = ?", (PANIC_META_KEY,))
            state.journal.execute("DELETE FROM meta WHERE key = ?", (PANIC_UNCLEAR_KEY,))
            # SAFE MODE снимается здесь же и тоже сначала в журнале (блок H, 2026-10-16)
            state.journal.execute("DELETE FROM meta WHERE key = ?", (SAFE_MODE_META_KEY,))
        if state.safe_mode_flag is not None:
            state.safe_mode_flag.unlink(missing_ok=True)  # второй носитель (ревью Codex R1)
        state.panic_at = None
        state.safe_mode = False
        state.safe_mode_unsaved = False
        state.safe_mode_record = None
    out = {"panic": False, "safe_mode": False, "paused": state.paused}
    if dropped:
        logger.warning("panic reset: unclear entries dropped by operator: %s", dropped)
        out["unclear_dropped"] = dropped
    return out


def panic_engaged(journal) -> str | None:
    """Момент последнего panic, если он не был сброшен оператором (переживает рестарт)."""
    row = journal.execute("SELECT value FROM meta WHERE key = ?", (PANIC_META_KEY,)).fetchone()
    return row[0] if row else None


# ------------------------------------------------- panic: входы с неясным исходом


def unclear_entries(journal) -> dict[str, dict]:
    """Входы panic с неясным исходом: {база cid: {"cid", "decision_id", "symbol", "side", "sl",
    "tp", "since", "why", "state"}}. state: unclear — исход не доказан, проверка по id; closed —
    исполнение доказано по clientOrderId и его qty закрыта, ждем записи исполнителя (учет без
    второго закрытия); manual — закрыть не удалось, только напоминания. Битая запись — пусто + лог."""
    row = journal.execute("SELECT value FROM meta WHERE key = ?", (PANIC_UNCLEAR_KEY,)).fetchone()
    if row is None:
        return {}
    try:
        data = json.loads(row[0])
    except ValueError as e:
        logger.error("panic unclear entries unreadable (%s): %r", e, row[0][:200])
        return {}
    return data if isinstance(data, dict) else {}


def _write_unclear(journal, entries: dict) -> None:
    if entries:
        journal.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                        (PANIC_UNCLEAR_KEY, json.dumps(entries, ensure_ascii=False, sort_keys=True)))
    else:
        journal.execute("DELETE FROM meta WHERE key = ?", (PANIC_UNCLEAR_KEY,))


def add_unclear_entry(journal, key: str, info: dict, state: RuntimeState | None = None) -> bool:
    """Записать вход в список неясных. True — новая запись (вызывающий шлет алерт один раз).
    panic уже снят оператором (state.panic_at is None) — не пишем: проверка под WRITE_LOCK, тем же,
    что держит reset_panic на журнал и память."""
    with transaction(journal):
        if state is not None and state.panic_at is None:
            return False
        entries = unclear_entries(journal)
        if key in entries:
            return False
        entries[key] = info
        _write_unclear(journal, entries)
    return True


def update_unclear_entry(journal, key: str, **fields) -> None:
    """Поля существующей записи; записи нет (снята reset'ом) — ничего."""
    with transaction(journal):
        entries = unclear_entries(journal)
        if key not in entries:
            return
        entries[key].update(fields)
        _write_unclear(journal, entries)


def drop_unclear_entry(journal, key: str) -> bool:
    """Исход доказан — запись снимается. True — была."""
    with transaction(journal):
        entries = unclear_entries(journal)
        if entries.pop(key, None) is None:
            return False
        _write_unclear(journal, entries)
    return True


# --------------------------------------------------------------- SAFE MODE


def _safe_mode_record(reason: str) -> str:
    return json.dumps({"reason": reason, "at": _now_iso()}, ensure_ascii=False)


def _parse_record(raw: str) -> dict:
    try:
        info = json.loads(raw)
        return {"reason": str(info.get("reason", "")), "at": info.get("at")}
    except (ValueError, AttributeError):
        return {"reason": str(raw), "at": None}  # битая запись — все равно SAFE MODE


def _write_flag_file(path: Path, record: str) -> None:
    """Атомарно: tmp + rename — недописанный файл не выглядит как «SAFE MODE снят»."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(record, encoding="utf-8")
    os.replace(tmp, path)


def read_safe_mode_flag(path: Path | None) -> dict | None:
    """{"reason", "at"} из маркер-файла; None — файла нет. Нечитаемый файл — все равно SAFE MODE."""
    if path is None or not path.exists():
        return None
    try:
        return _parse_record(path.read_text(encoding="utf-8"))
    except OSError as e:
        return {"reason": f"маркер-файл SAFE MODE не прочитан ({e})", "at": None}


def _persist_safe_mode(state: RuntimeState) -> bool:
    """Оба носителя из state.safe_mode_record: маркер-файл и meta. True — meta записана.
    Вызывается под _SAFE_MODE_LOCK; исключений не бросает."""
    record = state.safe_mode_record or _safe_mode_record("причина не записана")
    if state.safe_mode_flag is not None:
        try:
            _write_flag_file(state.safe_mode_flag, record)
        except OSError as e:
            logger.error("SAFE MODE flag file %s not written: %s", state.safe_mode_flag, e)
    try:
        with transaction(state.journal):
            state.journal.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (SAFE_MODE_META_KEY, record)
            )
    except Exception as e:  # noqa: BLE001 - флаг уже стоит; повтор — с тиков монитора и runner'а
        state.safe_mode_unsaved = True
        logger.error("SAFE MODE not persisted to journal (retry every tick; flag file %s): %s",
                     "written" if state.safe_mode_flag is not None else "off", e)
        return False
    state.safe_mode_unsaved = False
    return True


def engage_safe_mode(state: RuntimeState, reason: str) -> bool:
    """Взвести SAFE MODE: флаг в памяти ПЕРВЫМ, затем маркер-файл и запись в meta с причиной
    (переживают рестарт). meta не записалась — признак state.safe_mode_unsaved, повтор с тиков
    (retry_safe_mode_persist). True — взведен этим вызовом (алерт шлет вызывающий, один раз)."""
    with ENTRY_GATE_LOCK:  # допуск входа и взвод не перемежаются (ревью Codex H2/I)
        if state.safe_mode:
            return False
        state.safe_mode = True
    with _SAFE_MODE_LOCK:
        state.safe_mode_record = _safe_mode_record(reason)
        _persist_safe_mode(state)
    return True


def hold_safe_mode(state: RuntimeState, reason: str) -> bool:
    """SAFE MODE на обоих носителях, даже если флаг в памяти уже стоит: panic ставит его в памяти
    без записи (ревью H3, 3-й круг — неясный вход при panic: защита с причиной должна пережить и
    рестарт, и сбой записи panic). Уже записан в meta — False, причину не перетираем."""
    with ENTRY_GATE_LOCK:
        state.safe_mode = True
    with _SAFE_MODE_LOCK:
        try:
            if safe_mode_engaged(state.journal) is not None:
                return False
        except Exception as e:  # noqa: BLE001 - не прочитали: пишем (лишняя запись безвредна)
            logger.warning("SAFE MODE record unreadable (%s) - writing", e)
        state.safe_mode_record = _safe_mode_record(reason)
        _persist_safe_mode(state)
    return True


def retry_safe_mode_persist(state: RuntimeState | None) -> bool | None:
    """Тик монитора и runner'а (ревью Codex R1, 2026-10-16): SAFE MODE, не записанный в meta,
    пишется снова, пока не удастся. None — повторять нечего, True — записан этим вызовом.
    Исключений не бросает: тик не роняет."""
    if state is None or not getattr(state, "safe_mode_unsaved", False):
        return None
    try:
        with _SAFE_MODE_LOCK:
            if not state.safe_mode_unsaved or not state.safe_mode:
                return None  # снят оператором между проверкой и замком
            ok = _persist_safe_mode(state)
    except Exception as e:  # noqa: BLE001
        logger.error("SAFE MODE persist retry failed: %s", e)
        return False
    if ok:
        logger.warning("SAFE MODE persisted to journal on retry")
    return ok


def safe_mode_engaged(journal) -> dict | None:
    """{"reason", "at"} взведенного и не снятого SAFE MODE (переживает рестарт)."""
    row = journal.execute("SELECT value FROM meta WHERE key = ?", (SAFE_MODE_META_KEY,)).fetchone()
    if row is None:
        return None
    return _parse_record(row[0])


def restore_safe_mode(state: RuntimeState, notify=None) -> str | None:
    """Старт демона: SAFE MODE по ЛЮБОМУ носителю (meta или маркер-файл) -> флаг + алерт.
    Носители разошлись (сбой записи до рестарта) — недостающий дописывается; meta не
    записалась — повтор с тиков. Возвращает причину или None."""
    info = safe_mode_engaged(state.journal)
    flag = read_safe_mode_flag(state.safe_mode_flag)
    if info is None and flag is None:
        return None
    state.safe_mode = True
    src = info or flag
    reason = src["reason"] or "причина не записана"
    if info is None or (flag is None and state.safe_mode_flag is not None):
        with _SAFE_MODE_LOCK:
            state.safe_mode_record = json.dumps(
                {"reason": src["reason"], "at": src["at"]}, ensure_ascii=False)
            _persist_safe_mode(state)
    if info is None:
        logger.error("SAFE MODE restored from flag file only (journal had no record): %s", reason)
    if notify is not None:
        try:  # send_text шлет parse_mode=HTML, а в причине бывает текст ошибки биржи
            notify("🛑 " + html.escape(f"SAFE MODE восстановлен после рестарта: {reason}", quote=False))
        except Exception as e:  # noqa: BLE001 - канал не влияет на флаг
            logger.warning("notify failed: %s", e)
    return reason
