"""Ревью Codex ветки H3, 2-й круг (2026-10-16): четыре P1 в слежении chase и восстановлении учета.

Упрощение (3-й круг): система не закрывает и не берет под учет то, что не доказано своим по
идентификаторам; неоднозначность — SAFE MODE + алерт + решение оператора.

P1-1 — chase закрывал ручную позицию той же symbol/side после закрытия входа OSKAR;
P1-2 — восстановление на старте присваивало ручную позицию по оставшемуся SL решения;
P1-3 — chase не проверял биржу, пока исполнитель в процессе;
P1-4 — слежение терялось при рестарте, поздний fill шел в обычное сопровождение с защитой.
"""

import threading
from datetime import timedelta

from oskar.core import executor as executor_mod
from oskar.core.emergency import (
    add_unclear_entry,
    engage_panic,
    panic_engaged,
    reset_panic,
    unclear_entries,
)
from oskar.core.executor import make_client_order_id
from oskar.core.position_monitor import MonitorReport, PositionMonitor
from oskar.core.run_registry import register_run
from oskar.core.state import RuntimeState
from oskar.core.telegram_buttons import panic_reply
from oskar.execution.base import OrderState, PlacedOrder, PositionInfo, ProtectiveOrder, cid_base
from oskar.execution.bingx import BingXAdapter
from oskar.execution.paper import PaperAdapter
from tests.unit.test_capital_protection import h_plan, service, trades_count
from tests.unit.test_execution import FakeCcxt
from tests.unit.test_position_monitor import SYM, T0, add_trade, make_monitor, trade
from tests.unit.test_review_h2i import _add_decision, _fake_time, _in_thread


def _record_placements(ex) -> list:
    """place_sl/place_tp монитора — в список (новая защита при panic запрещена)."""
    placed: list = []

    def place(kind):
        def fn(symbol, side, price, cid):
            placed.append((kind, symbol, side, price))
            return PlacedOrder(f"{kind}-new", cid, kind.upper(), "open")
        return fn

    ex.place_sl, ex.place_tp = place("sl"), place("tp")
    return placed


def _admitted(key: str, target: tuple[str, str]) -> None:
    """Вход прошел допуск к отправке и висит (исполнитель в процессе)."""
    with executor_mod._INFLIGHT_LOCK:
        executor_mod._INFLIGHT.add(key)
        executor_mod._INFLIGHT_TARGETS[key] = target
        executor_mod._ADMITTED.add(key)


def _finished(key: str) -> None:
    with executor_mod._INFLIGHT_LOCK:
        executor_mod._INFLIGHT.discard(key)
        executor_mod._INFLIGHT_TARGETS.pop(key, None)
        executor_mod._ADMITTED.discard(key)


def _block_worker(ex, name):
    """Метод биржи висит только в потоке исполнителя (первый вызов), монитор идет свободно."""
    started, release = threading.Event(), threading.Event()
    orig = getattr(ex, name)
    main = threading.current_thread()

    def slow(*a, **k):
        if threading.current_thread() is not main and not started.is_set():
            started.set()
            release.wait(5)
        return orig(*a, **k)

    setattr(ex, name, slow)
    return started, release


# =========================== P1-1: ручная позиция той же symbol/side не закрывается


def test_panic_closes_only_own_qty_and_keeps_manual_part_and_its_stop(tmp_path):
    """Позиция пары = 2 OSKAR + 3 ручных (hedge mode BingX сливает их в одну). panic закрывает ровно
    qty сделки, снимает ее SL по id; ручной SL и ручной остаток живут, тики их не трогают."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "own", qty=2.0)
    ex.pos = [PositionInfo(SYM, "long", 5.0, 100.0, 0.0)]
    ex.protection = [
        ProtectiveOrder("own-sl", "sl", 98.0, "open", client_order_id="OSKAR-r1-own-sl", close_position=True),
        ProtectiveOrder("man-sl", "sl", 95.0, "open", qty=3.0),  # ручной стоп, без cid
    ]
    out = engage_panic(state, mon)
    assert out["exchange"]["closed"] == 1 and ex.closed_qty == [(SYM, "long", 2.0)]
    assert ex.pos == [PositionInfo(SYM, "long", 3.0, 100.0, 0.0)]
    assert "own-sl" in ex.cancelled and "man-sl" not in ex.cancelled
    assert (trade(j, "own")["status"], trade(j, "own")["close_reason"]) == ("closed", "panic")
    for _ in range(3):
        clock[0] += timedelta(seconds=61)
        mon.tick()
        mon.tick_pending()
    assert ex.closed_qty == [(SYM, "long", 2.0)] and ex.pos[0].qty == 3.0
    assert "man-sl" not in ex.cancelled


def test_manual_position_opened_after_late_entry_was_closed_is_untouched(tmp_path):
    """Сценарий Codex P1-1: вход OSKAR после окна закрыт по clientOrderId, исполнитель завершился;
    оператор открывает ручную позицию той же пары — раньше chase закрывал ее целиком минимум
    180 с. Теперь по паре ничего не закрывается: проверка — только по id входа."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    cid = make_client_order_id("r9", "SOLUSDTUSDT-7-long")
    key = cid_base(cid)
    ex.by_cid[cid] = OrderState("e1", "filled", 2.0, 100.0)
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 0.0)]
    _admitted(key, (SYM, "long"))
    try:
        _, sleep, clock_fn = _fake_time()
        out = engage_panic(state, mon, inflight_wait_sec=1.0, sleep=sleep, clock=clock_fn)
    finally:
        _finished(key)  # исполнитель завершился без записи (упал)
    assert out["exchange"]["closed"] == 1 and ex.closed_qty == [(SYM, "long", 2.0)] and ex.pos == []
    assert unclear_entries(j)[key]["state"] == "closed"  # исполнитель еще был в процессе
    ex.pos = [PositionInfo(SYM, "long", 4.0, 101.0, 0.0)]  # ручная позиция оператора
    for _ in range(4):
        clock[0] += timedelta(seconds=61)
        mon.tick_pending()
        mon.tick()
    assert ex.closed_qty == [(SYM, "long", 2.0)] and ex.pos[0].qty == 4.0
    assert unclear_entries(j) == {}  # исполнитель завершен, записи нет — учитывать нечего


def test_unclear_entry_never_closes_a_position_of_the_pair(tmp_path):
    """Вход не найден по clientOrderId: позиция пары (может быть ручной) не закрывается ни при panic,
    ни тиками; SAFE MODE на обоих носителях, один алерт с парой."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    cid = make_client_order_id("r9", "SOLUSDTUSDT-8-long")
    key = cid_base(cid)
    ex.pos = [PositionInfo(SYM, "long", 3.0, 100.0, 0.0)]
    _admitted(key, (SYM, "long"))
    try:
        _, sleep, clock_fn = _fake_time()
        out = engage_panic(state, mon, inflight_wait_sec=1.0, sleep=sleep, clock=clock_fn)
        assert out["exchange"]["unclear"] == [key]
        for _ in range(3):  # исполнитель еще ждет биржу
            clock[0] += timedelta(seconds=5)
            mon.tick_pending()
    finally:
        _finished(key)
    for _ in range(3):
        clock[0] += timedelta(seconds=61)
        mon.tick()
    assert ex.closed == [] and ex.pos == [PositionInfo(SYM, "long", 3.0, 100.0, 0.0)]
    assert state.safe_mode
    alerts = [n for n in notes if "неясным исходом" in n and "SOL/USDT:USDT long" in n]
    assert len(alerts) == 1 and "вручную" in alerts[0]
    assert unclear_entries(j) == {}  # за TTL ордера нет, исполнитель завершен — входа нет


# ==================== P1-2: старт не присваивает ручную позицию по оставшемуся SL решения


def test_startup_does_not_adopt_manual_position_by_leftover_own_stop(tmp_path):
    """Сценарий Codex P1-2: approved-решение без сделки и его собственный SL; исходную позицию
    закрыли вручную и открыли новую той же стороны. Раньше наличие SL сразу «подтверждало» вход:
    новая позиция шла в сопровождение по старому плану (с защитой и стопом по времени)."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    register_run(j, "r1")
    p = h_plan()
    _add_decision(j, p, resolution="approved")
    sl_cid = make_client_order_id("r1", p.candidate_id) + "-sl"
    ex.pos = [PositionInfo(p.symbol, "long", 1.0, 104.0, -3.0)]  # ручная, другой объем и цена
    ex.protection = [ProtectiveOrder("s1", "sl", p.sl, "open", client_order_id=sl_cid, close_position=True)]
    placed = _record_placements(ex)
    mon.startup_check()
    assert trades_count(j) == 0 and ex.cancelled == [] and ex.closed == []
    assert state.safe_mode
    alert = next(n for n in notes if "без сделки в журнале" in n)
    assert "qty 1" in alert and "104" in alert and sl_cid in alert and "вручную" in alert
    for _ in range(3):
        clock[0] += timedelta(hours=2)  # дольше max_loss_hold: стопа по времени нет — сделки нет
        mon.tick()
    assert placed == [] and ex.closed == [] and ex.cancelled == [] and trades_count(j) == 0


# ========================= P1-3: исполнитель в процессе не мешает проверке по id


def test_inflight_executor_does_not_block_check_by_client_id_and_no_double_close(tmp_path):
    """Сценарий Codex P1-3: окно panic истекло, биржа уже исполнила вход, а исполнитель висит на
    следующих операциях. Проверка по clientOrderId — сразу: закрыта ровно qty входа (ручная часть
    пары живет), его прикрепленная защита снята по уровням плана. Исполнитель позже записывает
    сделку — только учет, второго закрытия нет."""
    p = h_plan()
    svc, j, ex, state = service(tmp_path, p, price=[100.0])
    now = [T0]
    notes: list[str] = []
    mon = PositionMonitor(ex, j, max_loss_hold_seconds=3600, limit_entry_ttl_seconds=3600,
                          state=state, notify=notes.append, now_fn=lambda: now[0])
    cid = make_client_order_id("r1", p.candidate_id)
    key = cid_base(cid)
    orig_entry = ex.place_entry

    def entry_plus_manual(symbol, side, qty, client_order_id, **kw):
        placed = orig_entry(symbol, side, qty, client_order_id, **kw)
        ex.by_cid[client_order_id] = ex.orders[placed.exchange_id]  # биржа знает вход по cid
        ex.pos = [PositionInfo(symbol, side, qty + 1.5, 100.0, 0.0)]  # + ручные 1.5 той же пары
        return placed

    ex.place_entry = entry_plus_manual
    started, release = _block_worker(ex, "protection_orders")  # исполнитель: усыновление SL/TP
    worker, box = _in_thread(lambda: svc.execute(p.candidate_id, "market"))
    try:
        assert started.wait(5)
        qty = ex.by_cid[cid].filled_qty
        _, sleep, clock_fn = _fake_time()
        out = engage_panic(state, mon, inflight_wait_sec=1.0, sleep=sleep, clock=clock_fn)
        assert out["exchange"]["inflight_unfinished"] == [key]
        assert out["exchange"]["closed"] == 1 and ex.closed_qty == [(p.symbol, "long", qty)]
        assert ex.pos == [PositionInfo(p.symbol, "long", 1.5, 100.0, 0.0)]
        assert {("cancel", "att-sl"), ("cancel", "att-tp")} <= set(ex.calls)
        assert unclear_entries(j)[key]["state"] == "closed"
    finally:
        release.set()
        worker.join(5)
    assert box["out"].entry_sent and trade(j, p.candidate_id)["status"] == "open"  # записал позже
    now[0] += timedelta(seconds=5)
    mon.tick_pending()
    row = trade(j, p.candidate_id)
    assert (row["status"], row["close_reason"]) == ("closed", "panic")
    assert len(ex.closed_qty) == 1 and ex.pos[0].qty == 1.5  # второго закрытия нет
    assert unclear_entries(j) == {}
    assert any("исполнен после окна" in n for n in notes)


# =================== P1-4: рестарт не теряет неясные входы, при panic новой защиты нет


def test_unclear_entry_survives_restart_and_late_fill_is_closed_not_protected(tmp_path):
    """Сценарий Codex P1-4: panic оставил вход pending без id (исход неясен), демон перезапущен —
    panic восстановлен, список неясных — из meta. Поздний fill не идет в обычное сопровождение
    (раньше: open + новая защита) — закрыта ровно его qty."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "late", status="pending")
    out = engage_panic(state, mon)
    assert out["exchange"]["unclear"] == ["late"] and trade(j, "late")["status"] == "pending"
    # рестарт: новый процесс, флаг panic — из meta (как daemon.main)
    state2 = RuntimeState(journal=j)
    assert panic_engaged(j) is not None
    state2.panic()
    notes2: list[str] = []
    mon2 = PositionMonitor(ex, j, max_loss_hold_seconds=3600, limit_entry_ttl_seconds=3600,
                           notify=notes2.append, state=state2, now_fn=lambda: clock[0])
    placed = _record_placements(ex)
    mon2.startup_check()
    assert any("неясным исходом" in n and "late" in n for n in notes2)  # напоминание при старте
    assert mon2._has_pending()  # быстрый цикл — каждые 5 с
    ex.by_cid["OSKAR-r1-late"] = OrderState("e7", "filled", 2.0, 100.5, 0.1)  # биржа приняла позже
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.5, 0.0)]
    clock[0] += timedelta(seconds=5)
    mon2.tick_pending()
    row = trade(j, "late")
    assert (row["status"], row["close_reason"], row["qty"]) == ("closed", "panic", 2.0)
    assert ex.closed_qty == [(SYM, "long", 2.0)] and placed == []
    assert unclear_entries(j) == {}


def test_restored_panic_puts_no_new_protection_and_no_duration_stop(tmp_path):
    """panic восстановлен после рестарта, open-сделка без SL и с живой позицией: обычный монитор
    поставил бы SL (и закрыл бы лосера по времени). При panic — только учет по id и один алерт."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "naked")
    j.execute("UPDATE trades SET sl_order_id=NULL, tp_order_id=NULL")
    j.commit()
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, -5.0)]
    state.panic()
    placed = _record_placements(ex)
    clock[0] += timedelta(hours=4)  # дольше max_loss_hold (3 ч), позиция в минусе
    for _ in range(3):
        mon.tick_pending()
        mon.tick()
        clock[0] += timedelta(seconds=61)
    assert placed == [] and ex.closed == [] and trade(j, "naked")["status"] == "open"
    assert sum("новую защиту не ставлю" in n for n in notes) == 1
    ex.pos = []  # закрыта вручную — учет догоняет по бирже, как обычно
    for _ in range(2):
        clock[0] += timedelta(seconds=61)
        mon.tick()
    assert (trade(j, "naked")["status"], trade(j, "naked")["close_reason"]) == ("closed", "manual")


def test_no_new_protection_when_panic_lands_mid_tick(tmp_path):
    """Гонка: обычный тик уже начался (pending -> исполнен), а оператор взвел panic. Перевод в open
    по бирже — да, новая защита — нет (_place_protection проверяет panic сам)."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "lim", status="pending")
    j.execute("UPDATE trades SET sl_order_id=NULL, tp_order_id=NULL")
    j.commit()
    ex.by_cid["OSKAR-r1-lim"] = OrderState("e1", "filled", 2.0, 99.5, 0.1)
    ex.pos = [PositionInfo(SYM, "long", 2.0, 99.5, 0.0)]
    placed = _record_placements(ex)
    state.panic()
    mon._check_pending(trade(j, "lim"), MonitorReport())
    assert trade(j, "lim")["status"] == "open" and placed == []


# ========================================================== прочее консервативное


def test_failed_panic_close_is_not_retried_automatically(tmp_path):
    """Закрытие упало (таймаут: запрос мог дойти до биржи) — ни финальный проход, ни тики его не
    повторяют: повтор закрыл бы ручную часть пары. Алерт один; повторный /panic — пробует снова."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "own", qty=2.0)
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 0.0)]
    calls = []

    def timeout(symbol, side, qty):
        calls.append(qty)
        raise TimeoutError("bingx request timeout")

    ex.close_qty = timeout
    out = engage_panic(state, mon)
    assert calls == [2.0] and out["exchange"]["closed"] == 0 and out["exchange"]["errors"]
    for _ in range(3):
        clock[0] += timedelta(seconds=61)
        mon.tick()
        mon.tick_pending()
    assert calls == [2.0] and trade(j, "own")["status"] == "open"
    assert sum("закрыть не удалось" in n for n in notes) == 1
    assert not any("новую защиту не ставлю" in n for n in notes)  # тот же случай — без дубля
    engage_panic(state, mon)  # оператор повторил /panic
    assert calls == [2.0, 2.0]


def test_unclear_list_is_not_resurrected_after_reset(tmp_path):
    j = make_monitor(tmp_path)[1]
    state = RuntimeState(journal=j)
    state.panic()
    assert add_unclear_entry(j, "oskar-r1-a", {"symbol": SYM, "side": "long"}, state)
    assert not add_unclear_entry(j, "oskar-r1-a", {"symbol": SYM, "side": "long"}, state)  # дедуп
    assert reset_panic(state)["unclear_dropped"] == ["oskar-r1-a"]
    assert not add_unclear_entry(j, "oskar-r1-b", {"symbol": SYM, "side": "long"}, state)
    assert unclear_entries(j) == {}


def test_panic_reply_names_unclear_entries():
    text = panic_reply({"exchange": {"cancelled": 0, "closed": 1, "errors": [], "unclear": ["D-1"]}})
    assert "неясным исходом: 1" in text and "D-1" in text and "вручную" in text


# ======================================================= адаптеры: close_qty


def test_bingx_close_qty_closes_only_given_qty_by_position_side():
    """Закрывающая сторона + positionSide (reduceOnly в hedge mode BingX запрещен), объем — не
    больше позиции; позиции нет — ордера нет."""
    fake = FakeCcxt()
    fake._positions = [{"symbol": "SOL-USDT", "side": "long", "contracts": 5.0, "contractSize": 1.0,
                        "entryPrice": 100.0, "unrealizedPnl": 0.0}]
    ad = BingXAdapter(fake)
    assert ad.close_qty("SOL/USDT:USDT", "long", 2.0) is not None
    o = fake.orders[0]
    assert (o["type"], o["side"], o["amount"]) == ("market", "sell", 2.0)
    assert o["params"] == {"positionSide": "LONG"} and "reduceOnly" not in o["params"]
    ad.close_qty("SOL/USDT:USDT", "long", 9.0)
    assert fake.orders[1]["amount"] == 5.0  # не больше позиции
    assert ad.close_qty("SOL/USDT:USDT", "short", 1.0) is None and len(fake.orders) == 2


def test_paper_close_qty_partial_keeps_rest():
    ad = PaperAdapter(lambda s: 101.0)
    ad.place_entry("SOL/USDT:USDT", "long", 5.0, "OSKAR-r1-x")
    ad.close_qty("SOL/USDT:USDT", "long", 2.0)
    assert [(p.side, p.qty) for p in ad.positions()] == [("long", 3.0)]
    ad.close_qty("SOL/USDT:USDT", "long", 9.0)
    assert ad.positions() == [] and ad.close_qty("SOL/USDT:USDT", "long", 1.0) is None
