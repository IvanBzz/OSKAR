"""Ревью ветки H3, 4-й круг (2026-10-16): один P1 и два P2 в консервативном panic.

P1-1 — повторный /panic (второй канал или повторное нажатие) закрывал по clientOrderId вход, уже
       закрытый первым, и резал ручную позицию той же пары; то же — когда запись состояния в meta
       не удалась (тик раз в 5 с повторял close_qty, пока исполнитель в процессе);
P2-1 — panic закрывал qty open-сделки, не проверив по id, не сработал ли уже ее SL/TP (журнал
       узнает об этом только на полном тике) — close_qty срезал ручную часть пары;
P2-2 — тик монитора под panic держал _FLATTEN_LOCK на всех сетевых вызовах полного тика, и
       /panic его ждал.
"""

import threading
import time
from datetime import timedelta

from oskar.core import emergency as emergency_mod
from oskar.core import position_monitor as monitor_mod
from oskar.core.emergency import (
    engage_panic,
    flatten_wanted,
    safe_mode_engaged,
    unclear_entries,
    upsert_unclear_entry,
)
from oskar.core.executor import make_client_order_id
from oskar.core.state import RuntimeState
from oskar.execution.base import OrderState, PositionInfo, cid_base
from tests.unit.test_position_monitor import SYM, add_trade, make_monitor, trade
from tests.unit.test_review_h2i import _fake_time, _in_thread
from tests.unit.test_review_h3_conservative import _admitted, _block_worker, _finished
from tests.unit.test_review_r1 import FlakyJournal


def _panic(state, mon):
    """engage_panic с окном ожидания 1 с по фальшивым часам (исполнитель висит дольше)."""
    _, sleep, clock_fn = _fake_time()
    return engage_panic(state, mon, inflight_wait_sec=1.0, sleep=sleep, clock=clock_fn)


def _late_entry(n: int) -> tuple[str, str]:
    cid = make_client_order_id("r9", f"SOLUSDTUSDT-{n}-long")
    return cid, cid_base(cid)


# ================== P1-1: повторный /panic не закрывает уже закрытый вход второй раз


def test_second_panic_does_not_close_entry_already_closed_by_client_id(tmp_path):
    """Сценарий A ревью: первый /panic — исполнитель висит дольше окна, по clientOrderId вход
    исполнен на 2, закрыто 2 (state closed). Второй /panic (Telegram и API, повторное нажатие) —
    исполнитель все еще в процессе. Позиция «2 OSKAR + 3 ручных» должна остаться 3, а не 1."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    cid, key = _late_entry(21)
    ex.by_cid[cid] = OrderState("e1", "filled", 2.0, 100.0)
    ex.pos = [PositionInfo(SYM, "long", 5.0, 100.0, 0.0)]  # 2 OSKAR + 3 ручных
    _admitted(key, (SYM, "long"))
    try:
        first = _panic(state, mon)
        assert first["exchange"]["closed"] == 1 and unclear_entries(j)[key]["state"] == "closed"
        second = _panic(state, mon)
        assert second["exchange"]["closed"] == 0 and not second["exchange"].get("unclear")
        assert ex.closed_qty == [(SYM, "long", 2.0)] and ex.pos[0].qty == 3.0
        assert unclear_entries(j)[key]["state"] == "closed"
        for _ in range(3):  # исполнитель еще в процессе — тики тоже не закрывают
            clock[0] += timedelta(seconds=5)
            mon.tick_pending()
        assert ex.closed_qty == [(SYM, "long", 2.0)] and ex.pos[0].qty == 3.0
    finally:
        _finished(key)


def test_concurrent_panics_from_two_channels_close_entry_once(tmp_path):
    """Telegram и API одновременно: вторые флаги ставятся сразу, биржевая часть ждет первую — и
    видит сохраненную запись closed, а не новую unclear."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    cid, key = _late_entry(25)
    ex.by_cid[cid] = OrderState("e1", "filled", 2.0, 100.0)
    ex.pos = [PositionInfo(SYM, "long", 5.0, 100.0, 0.0)]
    _admitted(key, (SYM, "long"))
    try:
        workers = [threading.Thread(target=lambda: _panic(state, mon)) for _ in range(2)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(5)
        assert not any(w.is_alive() for w in workers)
        assert ex.closed_qty == [(SYM, "long", 2.0)] and ex.pos[0].qty == 3.0
    finally:
        _finished(key)


def test_second_panic_leaves_manual_entry_to_operator(tmp_path):
    """Закрыть вход по clientOrderId не удалось (запрос мог дойти до биржи) — state manual.
    Повторный /panic его не трогает (решение оператора), но называет в сводке неясных."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    cid, key = _late_entry(26)
    ex.by_cid[cid] = OrderState("e1", "filled", 2.0, 100.0)
    ex.pos = [PositionInfo(SYM, "long", 5.0, 100.0, 0.0)]
    calls = []

    def timeout(symbol, side, qty):
        calls.append(qty)
        raise TimeoutError("bingx request timeout")

    ex.close_qty = timeout
    _admitted(key, (SYM, "long"))
    try:
        _panic(state, mon)
        assert calls == [2.0] and unclear_entries(j)[key]["state"] == "manual"
        second = _panic(state, mon)
        assert calls == [2.0] and second["exchange"]["unclear"] == [key]
    finally:
        _finished(key)


def test_second_panic_closes_unclear_entry_once_and_saves_closed_state(tmp_path):
    """Сценарий B ревью: первый /panic — ордер еще не виден (запись unclear). Второй /panic видит
    fill и закрывает, но state в meta оставался unclear (insert не обновлял ключ) — следующий
    tick_pending закрывал еще раз. Теперь запись — upsert: closed, второго закрытия нет."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    cid, key = _late_entry(22)
    ex.pos = [PositionInfo(SYM, "long", 3.0, 100.0, 0.0)]  # ручная позиция пары
    _admitted(key, (SYM, "long"))
    try:
        assert _panic(state, mon)["exchange"]["unclear"] == [key]
        assert unclear_entries(j)[key]["state"] == "unclear"
        ex.by_cid[cid] = OrderState("e1", "filled", 2.0, 100.0)  # биржа приняла вход позже
        ex.pos = [PositionInfo(SYM, "long", 5.0, 100.0, 0.0)]
        assert _panic(state, mon)["exchange"]["closed"] == 1
        assert unclear_entries(j)[key]["state"] == "closed"
        for _ in range(3):
            clock[0] += timedelta(seconds=5)
            mon.tick_pending()
        assert ex.closed_qty == [(SYM, "long", 2.0)] and ex.pos[0].qty == 3.0
    finally:
        _finished(key)


def test_second_panic_with_unsaved_closed_state_does_not_reclose(tmp_path):
    """Запись closed в meta не удалась (SQLite занят): повторный /panic и тики помнят закрытие в
    памяти процесса — по clientOrderId второй раз не закрывают."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    mon._j = FlakyJournal(j)  # запись журнала монитора падает, чтение идет
    cid, key = _late_entry(23)
    ex.by_cid[cid] = OrderState("e1", "filled", 2.0, 100.0)
    ex.pos = [PositionInfo(SYM, "long", 5.0, 100.0, 0.0)]
    _admitted(key, (SYM, "long"))
    try:
        assert _panic(state, mon)["exchange"]["closed"] == 1
        assert unclear_entries(j) == {}  # не записано
        _panic(state, mon)
        for _ in range(3):
            clock[0] += timedelta(seconds=5)
            mon.tick_pending()
        assert ex.closed_qty == [(SYM, "long", 2.0)] and ex.pos[0].qty == 3.0
    finally:
        _finished(key)


def test_tick_does_not_reclose_when_closed_state_update_fails(tmp_path):
    """Тот же корень на тике: вход закрыт по clientOrderId, а update записи (state closed) упал —
    раньше каждый тик раз в 5 с повторял close_qty, пока исполнитель в процессе."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    cid, key = _late_entry(24)
    ex.pos = [PositionInfo(SYM, "long", 3.0, 100.0, 0.0)]
    _admitted(key, (SYM, "long"))
    try:
        assert _panic(state, mon)["exchange"]["unclear"] == [key]  # ордер еще не виден
        ex.by_cid[cid] = OrderState("e1", "filled", 2.0, 100.0)
        ex.pos = [PositionInfo(SYM, "long", 5.0, 100.0, 0.0)]

        def broken(*a, **k):
            raise RuntimeError("database is locked")

        orig = monitor_mod.update_unclear_entry
        monitor_mod.update_unclear_entry = broken
        try:
            for _ in range(4):
                clock[0] += timedelta(seconds=5)
                mon.tick_pending()
        finally:
            monitor_mod.update_unclear_entry = orig
        assert ex.closed_qty == [(SYM, "long", 2.0)] and ex.pos[0].qty == 3.0
        clock[0] += timedelta(seconds=5)
        mon.tick_pending()  # запись снова работает — состояние догоняет память
        assert unclear_entries(j)[key]["state"] == "closed" and len(ex.closed_qty) == 1
    finally:
        _finished(key)


def test_resolve_inflight_books_recorded_trade_of_closed_entry_without_second_close(tmp_path):
    """Исполнитель записал сделку между финальным проходом и проверкой по clientOrderId второго
    /panic: путь сделок получает состояние сохраненной записи (closed) — только учет, а не
    закрытие qty (раньше ему передавался пустой список и сделка закрывалась второй раз)."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    key = "oskar-r9-d1"
    state.panic()
    upsert_unclear_entry(j, key, {"cid": "OSKAR-r9-d1", "decision_id": "d1", "symbol": SYM,
                                  "side": "long", "state": "closed", "closed_qty": 2.0}, state)
    add_trade(j, "d1", run_id="r9", qty=2.0)  # исполнитель записал позже
    ex.pos = [PositionInfo(SYM, "long", 3.0, 100.0, 0.0)]  # остался ручной остаток
    mon.panic_begin()
    out = mon.panic_resolve_inflight({key: (SYM, "long")})
    assert ex.closed_qty == [] and out["closed"] == 0
    row = trade(j, "d1")
    assert (row["status"], row["close_reason"]) == ("closed", "panic")
    assert unclear_entries(j) == {}


def test_upsert_unclear_entry_updates_existing_and_respects_reset(tmp_path):
    j = make_monitor(tmp_path)[1]
    state = RuntimeState(journal=j)
    state.panic()
    assert upsert_unclear_entry(j, "oskar-r1-a", {"symbol": SYM, "state": "unclear"}, state)
    assert not upsert_unclear_entry(j, "oskar-r1-a", {"state": "closed"}, state)
    assert unclear_entries(j)["oskar-r1-a"] == {"symbol": SYM, "state": "closed"}
    state.panic_at = None  # снят оператором — не воскрешаем
    assert not upsert_unclear_entry(j, "oskar-r1-b", {"state": "closed"}, state)
    assert "oskar-r1-b" not in unclear_entries(j)


# ========== P2-1: panic не закрывает сделку, чей SL/TP уже сработал (проверка по id)


def test_panic_books_sl_hit_by_id_instead_of_closing_manual_part(tmp_path):
    """SL сделки сработал за 20 с до /panic, журнал еще не знает (полный тик — раз в минуту); на
    паре — ручная позиция 3. Раньше close_qty(2) срезал ручную часть; теперь — учет sl_hit по id."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "own", qty=2.0)
    ex.orders["own-sl"] = OrderState("own-sl", "filled", 2.0, 97.9, 0.1)
    ex.pos = [PositionInfo(SYM, "long", 3.0, 100.0, 0.0)]  # ручная
    out = engage_panic(state, mon)
    assert ex.closed_qty == [] and ex.pos[0].qty == 3.0
    assert out["exchange"]["closed"] == 0 and not out["exchange"]["errors"]
    row = trade(j, "own")
    assert (row["status"], row["close_reason"], row["exit_price"]) == ("closed", "sl_hit", 97.9)
    assert "own-tp" in ex.cancelled  # парный TP снят по id


def test_panic_does_not_close_when_protection_check_fails_then_retries(tmp_path):
    """Проверка SL/TP по id не прошла (сеть) — не доказано, что позиция еще своя: не закрываем,
    SAFE MODE на обоих носителях и один алерт. На бирже ничего не сделано — повтор безопасен:
    финальный проход того же panic закрывает, когда биржа ответила."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "own", qty=2.0)
    ex.pos = [PositionInfo(SYM, "long", 5.0, 100.0, 0.0)]
    ex.fail_symbols = {SYM}  # order_status падает
    out = engage_panic(state, mon)
    assert ex.closed_qty == [] and trade(j, "own")["status"] == "open"
    assert len(out["exchange"]["errors"]) == 1  # один раз на panic, а не на каждый проход
    assert sum("SL/TP по id не проверены" in n for n in notes) == 1
    assert safe_mode_engaged(j) is not None
    for _ in range(2):  # тики не закрывают и не дублируют алерт
        clock[0] += timedelta(seconds=61)
        mon.tick()
    assert ex.closed_qty == [] and not any("новую защиту не ставлю" in n for n in notes)

    calls = []
    orig = ex.order_status

    def flaky_once(symbol, exchange_id="", client_order_id=""):
        calls.append(exchange_id)
        if len(calls) == 1:
            raise RuntimeError("exchange hiccup")
        return orig(symbol, exchange_id=exchange_id, client_order_id=client_order_id)

    ex.fail_symbols = set()
    ex.order_status = flaky_once
    out = engage_panic(state, mon)  # оператор повторил /panic: первый проход — сбой, финальный — ок
    assert ex.closed_qty == [(SYM, "long", 2.0)] and ex.pos[0].qty == 3.0
    assert out["exchange"]["closed"] == 1
    assert trade(j, "own")["close_reason"] == "panic"


# ============== P2-2: тик монитора под panic не держит _FLATTEN_LOCK на сетевых вызовах


def test_panic_does_not_wait_for_full_tick_network_calls(tmp_path):
    """Полный тик под panic проверяет open-сделку (order_status по id висит на таймауте BingX).
    Повторный /panic не ждет этот тик: учет open-сделок под panic идет вне _FLATTEN_LOCK."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "own", qty=2.0)
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 0.0)]
    state.panic()
    started, release = _block_worker(ex, "order_status")
    ticker, _ = _in_thread(mon.tick)
    try:
        assert started.wait(5)  # тик висит на сети
        assert not emergency_mod._FLATTEN_LOCK.locked()
        panicker, box = _in_thread(lambda: engage_panic(state, mon))
        panicker.join(3)
        assert not panicker.is_alive(), "/panic ждал тик монитора"
        assert box["out"]["exchange"]["closed"] == 1
    finally:
        release.set()
        ticker.join(5)
    assert ex.closed_qty == [(SYM, "long", 2.0)]
    row = trade(j, "own")
    assert (row["status"], row["close_reason"]) == ("closed", "panic")  # тик не перетер учет
    assert not any("новую защиту не ставлю" in n for n in notes)  # и не алертил о закрытой


def test_panic_tick_yields_flatten_lock_between_items(tmp_path):
    """Под замком тик ведет то, что может закрыть по id (неясные входы, pending), — по одному
    элементу: /panic, ждущий замок, получает его после текущего элемента, а не после всех."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "p1", status="pending")
    add_trade(j, "p2", status="pending")
    state.panic()
    seen: list[tuple[str, str]] = []
    orig = ex.order_status

    def record(symbol, exchange_id="", client_order_id=""):
        seen.append((threading.current_thread().name, client_order_id or exchange_id))
        return orig(symbol, exchange_id=exchange_id, client_order_id=client_order_id)

    ex.order_status = record
    started, release = _block_worker(ex, "order_status")
    ticker = threading.Thread(target=mon.tick_pending, name="tick")
    ticker.start()
    try:
        assert started.wait(5) and emergency_mod._FLATTEN_LOCK.locked()
        panicker = threading.Thread(target=lambda: engage_panic(state, mon), name="panic")
        panicker.start()
        deadline = time.monotonic() + 5
        while not flatten_wanted() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert flatten_wanted()
    finally:
        release.set()
    ticker.join(5)
    panicker.join(5)
    assert not ticker.is_alive() and not panicker.is_alive()
    assert ("tick", "OSKAR-r1-p2") not in seen  # второй элемент тик уступил panic
    assert ("panic", "OSKAR-r1-p2") in seen
    assert not flatten_wanted()
