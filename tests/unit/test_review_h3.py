"""Ревью Codex (gpt-6-astra) ветки блока H3 (2026-10-16): одна P1 и три P2.

P1 — panic оставлял открытой позицию входа, завершившегося внутри окна ожидания со статусом
     pending: по таймауту без id биржи (финальный проход ставил cancelled, слежения нет — left
     пуст) или уже исполненного (запись cancelled, позиция живет, записанные SL/TP сняты);
P2-1 — после panic-reset SAFE MODE из-за чужих ордеров возвращался только со сканом сирот (до 10 мин);
P2-2 — старт восстанавливал учет без подтверждения биржи: брал под учет позицию второй машины
       с тем же сетапом (или ручную);
P2-3 — свой SL/TP-сирота сохранялся по совпадению символа без учета стороны и без алерта.
"""

import threading
from datetime import timedelta

import pytest

from oskar.core import executor as executor_mod
from oskar.core.emergency import engage_panic, reset_panic
from oskar.core.executor import KIND_ENTRY_UNKNOWN, make_client_order_id
from oskar.core.position_monitor import UNKNOWN_ENTRY_TTL_SEC, PositionMonitor
from oskar.core.run_registry import register_run
from oskar.execution.base import (
    OpenOrderRef,
    OrderState,
    PositionInfo,
    ProtectiveOrder,
    cid_base,
)
from tests.unit.test_capital_protection import h_plan, service, trades_count
from tests.unit.test_position_monitor import SYM, T0, add_trade, make_monitor, trade
from tests.unit.test_review_h2i import _add_decision, _in_thread
from tests.unit.test_second_machine import FOREIGN

# ============================== P1: panic и вход, завершившийся внутри окна со статусом pending


def test_panic_keeps_and_chases_entry_that_timed_out_inside_window(tmp_path):
    """Сценарий ревью: place_entry падает по таймауту (ccxt 10 с < окна 15 с), по clientOrderId
    ордера нет — вход записан pending без id биржи, исполнитель вышел из реестра внутри окна
    (left пуст). Финальный проход ордер не находит — раньше ставил cancelled, а биржа принимала
    вход позже: позиция жила мимо журнала, закрывать ее было некому."""
    p = h_plan()
    svc, j, ex, state = service(tmp_path, p, price=[100.0])
    now = [T0]
    mon = PositionMonitor(ex, j, max_loss_hold_seconds=3600, limit_entry_ttl_seconds=3600,
                          state=state, now_fn=lambda: now[0])
    sent, release = threading.Event(), threading.Event()

    def timeout_entry(*a, **k):
        sent.set()
        release.wait(5)
        raise TimeoutError("bingx request timeout")  # как ccxt RequestTimeout: исход неизвестен

    ex.place_entry = timeout_entry
    worker, box = _in_thread(lambda: svc.execute(p.candidate_id, "market"))
    assert sent.wait(5)
    t = [0.0]

    def sleep(s):  # исполнитель завершается внутри окна ожидания
        release.set()
        worker.join(5)
        t[0] += s

    out = engage_panic(state, mon, inflight_wait_sec=15.0, sleep=sleep, clock=lambda: t[0])
    assert box["out"].kind == KIND_ENTRY_UNKNOWN
    assert "inflight_unfinished" not in out["exchange"]  # left пуст
    assert trade(j, p.candidate_id)["status"] == "pending"  # исход неизвестен — не «снят»
    assert out["exchange"]["watching"] == [p.candidate_id]
    cid = make_client_order_id("r1", p.candidate_id)
    assert mon.chasing() == [cid_base(cid)]
    # биржа приняла вход уже после panic
    ex.by_cid[cid] = OrderState("e9", "filled", 3.0, 100.2, 0.05)
    ex.pos = [PositionInfo(p.symbol, "long", 3.0, 100.2, 0.0)]
    for _ in range(3):
        now[0] += timedelta(seconds=5)
        mon.tick_pending()
    row = trade(j, p.candidate_id)
    assert (row["status"], row["close_reason"], row["qty"]) == ("closed", "panic", 3.0)
    assert ex.pos == [] and ("close", p.symbol, "long") in ex.calls


def test_panic_closes_filled_pending_entry_and_its_protection(tmp_path):
    """Лимитка исполнилась между тиками монитора — в журнале еще pending. panic ставил записи
    cancelled и снимал записанные SL/TP: позиция оставалась открытой и без стопа."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "lim", status="pending")  # sl/tp в журнале: lim-sl / lim-tp
    ex.by_cid["OSKAR-r1-lim"] = OrderState("e1", "filled", 2.0, 99.5, 0.1)
    ex.pos = [PositionInfo(SYM, "long", 2.0, 99.5, 0.0)]
    ex.protection = [ProtectiveOrder("att-sl", "sl", 98.0, "open", qty=2.0)]  # прикрепленный
    out = engage_panic(state, mon)
    row = trade(j, "lim")
    assert (row["status"], row["close_reason"], row["entry_price"]) == ("closed", "panic", 99.5)
    assert ex.closed == [(SYM, "long")] and ex.pos == []
    assert {"lim-sl", "lim-tp", "att-sl"} <= set(ex.cancelled)
    assert out["exchange"]["closed"] == 1


def test_panic_partial_fill_cancels_rest_and_closes_position(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "part", status="pending")
    ex.by_cid["OSKAR-r1-part"] = OrderState("e1", "open", 1.0, 100.0)
    ex.orders["e1"] = OrderState("e1", "canceled", 1.0, 100.0, 0.05)  # после отмены остатка
    ex.pos = [PositionInfo(SYM, "long", 1.0, 100.0, 0.0)]
    engage_panic(state, mon)
    row = trade(j, "part")
    assert (row["status"], row["close_reason"], row["qty"]) == ("closed", "panic", 1.0)
    assert "e1" in ex.cancelled and ex.closed == [(SYM, "long")]


@pytest.mark.parametrize("lookup", ["missing", "hiccup"])
def test_panic_unknown_pending_entry_is_watched_not_cancelled_silently(tmp_path, lookup):
    """Ордер не найден (таймаут на входе) или биржа не ответила — запись остается pending, вход —
    на слежении по cid и symbol/side; снимается с учета, когда исход стал известен: ордера нет
    за TTL входа с неизвестным исходом или он снят биржей."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "lost", status="pending")
    if lookup == "hiccup":
        ex.fail_symbols = {SYM}
    out = engage_panic(state, mon)
    assert trade(j, "lost")["status"] == "pending"
    assert out["exchange"]["watching"] == ["lost"] and out["exchange"]["cancelled"] == 0
    assert mon.chasing() == ["oskar-r1-lost"]
    clock[0] += timedelta(seconds=5)
    mon.tick_pending()
    assert trade(j, "lost")["status"] == "pending"
    if lookup == "missing":
        clock[0] += timedelta(seconds=UNKNOWN_ENTRY_TTL_SEC)
    else:
        ex.fail_symbols = set()
        ex.by_cid["OSKAR-r1-lost"] = OrderState("e1", "canceled")
    mon.tick_pending()
    assert trade(j, "lost")["status"] == "cancelled" and ex.closed == []
    for _ in range(3):
        clock[0] += timedelta(minutes=2)
        mon.tick_pending()
    assert mon.chasing() == []


def test_panic_chases_every_entry_admitted_at_engage_even_if_done_inside_window(tmp_path):
    """Вход, допущенный к отправке до panic, завершился внутри окна (реестр пуст, left пуст), а
    финальный проход позицию не застал (биржа еще не показала ее). Слежение — за всеми входами,
    допущенными на момент взвода, а не только за незавершенными к концу окна."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    key = cid_base(make_client_order_id("r9", "SOLUSDTUSDT-7-long"))
    with executor_mod._INFLIGHT_LOCK:
        executor_mod._INFLIGHT.add(key)
        executor_mod._INFLIGHT_TARGETS[key] = (SYM, "long")
        executor_mod._ADMITTED.add(key)
    t = [0.0]

    def sleep(s):  # исполнитель вышел из реестра внутри окна
        with executor_mod._INFLIGHT_LOCK:
            executor_mod._INFLIGHT.discard(key)
            executor_mod._INFLIGHT_TARGETS.pop(key, None)
            executor_mod._ADMITTED.discard(key)
        t[0] += s

    try:
        out = engage_panic(state, mon, inflight_wait_sec=15.0, sleep=sleep, clock=lambda: t[0])
    finally:
        sleep(0)
    assert "inflight_unfinished" not in out["exchange"]
    assert mon.chasing() == [key]
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 0.0)]  # биржа приняла вход позже
    clock[0] += timedelta(seconds=5)
    mon.tick_pending()
    assert ex.closed == [(SYM, "long")] and ex.pos == []


# ======================== P2-1: SAFE MODE после reset при чужих ордерах — на следующем тике


def test_foreign_orders_reengage_safe_mode_on_next_fast_tick_after_reset(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    ex.open_list = [OpenOrderRef(SYM, "f1", FOREIGN + "-sl", "sl")]
    mon.tick()
    assert state.safe_mode
    reset_panic(state)  # ордера второй машины остались
    assert mon._has_pending()  # быстрый цикл (5 с) не ждет ни сделок, ни полного тика
    clock[0] += timedelta(seconds=5)
    mon.tick_pending()
    assert state.safe_mode and ex.cancelled == []


def test_foreign_orders_reengage_safe_mode_on_next_full_tick_after_reset(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    ex.open_list = [OpenOrderRef(SYM, "f1", FOREIGN + "-sl", "sl")]
    mon.tick()
    reset_panic(state)
    clock[0] += timedelta(seconds=61)  # меньше ORPHAN_SCAN_SEC
    mon.tick()
    assert state.safe_mode


def test_no_forced_rescan_once_foreign_orders_are_gone(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    ex.open_list = [OpenOrderRef(SYM, "f1", FOREIGN + "-sl", "sl")]
    mon.tick()
    reset_panic(state)
    ex.open_list = []  # вторая машина остановлена, ее ордера сняты
    mon.tick_pending()
    assert not state.safe_mode and not mon._has_pending()


# ================================ P2-2: старт восстанавливает учет только с подтверждением биржи


def test_startup_does_not_restore_position_with_same_setup_orders_of_unknown_run(tmp_path):
    """Тот же сетап на второй машине: SL с хэшем этого решения, но от незнакомого запуска.
    Раньше такие ордера пропускались, и сделка восстанавливалась по догадке — монитор вел бы
    чужую позицию (дубли SL/TP, стоп по времени)."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    register_run(j, "r1")
    p = h_plan()
    _add_decision(j, p, resolution="approved")
    other = make_client_order_id("ffff0000", p.candidate_id)
    ex.pos = [PositionInfo(p.symbol, "long", 3.0, 100.1, 0.0)]
    ex.open_list = [OpenOrderRef(p.symbol, "s2", other + "-sl", "sl")]
    ex.protection = [ProtectiveOrder("s2", "sl", p.sl, "open", client_order_id=other + "-sl",
                                     close_position=True)]
    ex.by_cid[make_client_order_id("r1", p.candidate_id)] = OrderState("e1", "filled", 3.0, 100.1)
    mon.startup_check()
    assert trades_count(j) == 0 and ex.cancelled == [] and ex.closed == []
    assert state.safe_mode and any("чужого запуска" in n for n in notes)


def test_startup_restores_lost_trade_only_with_exchange_confirmation(tmp_path):
    """SL/TP прикреплены ко входу (без cid) — своей защиты по cid не видно. Учет восстанавливается,
    только если вход найден по clientOrderId известного запуска, исполнен и qty совпадает."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    register_run(j, "r1")
    p = h_plan()
    _add_decision(j, p, resolution="approved")
    cid = make_client_order_id("r1", p.candidate_id)
    ex.pos = [PositionInfo(p.symbol, "long", 3.0, 100.1, 0.0)]
    ex.protection = [ProtectiveOrder("a1", "sl", p.sl, "open", qty=3.0),
                     ProtectiveOrder("a2", "tp", p.tp, "open", qty=3.0)]
    mon.startup_check()
    assert trades_count(j) == 0 and state.safe_mode and ex.closed == []  # вход не найден
    ex.by_cid[cid] = OrderState("e1", "filled", 1.0, 100.1)  # наш вход 1, на бирже 3: не только наша
    mon.startup_check()
    assert trades_count(j) == 0
    ex.by_cid[cid] = OrderState("e1", "filled", 3.0, 100.1)
    mon.startup_check()
    row = trade(j, p.candidate_id)
    assert row is not None and row["client_order_id"] == cid and row["status"] == "open"
    assert (row["sl_order_id"], row["tp_order_id"]) == ("a1", "a2") and ex.cancelled == []


# ========================== P2-3: свой SL/TP-сирота — по паре symbol/side и с алертом


def _sided_protection(ex, sides: dict[str, str]) -> None:
    """protection_orders по стороне позиции, как у BingX в hedge mode: {id ордера: side}."""
    def by_side(symbol, side, *, include_closed=False):
        return [o for o in ex.protection
                if sides.get(o.exchange_id) == side and (include_closed or o.status == "open")]

    ex.protection_orders = by_side


def test_own_orphan_protection_of_other_side_is_cancelled_despite_untracked_position(tmp_path):
    """Ручной short на SOL без сделки и старый closePosition-SL OSKAR для long (отмена не прошла).
    Раньше он сохранялся бессрочно по символу и закрыл бы следующую long-сделку по старой цене."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    register_run(j, "r1")
    ex.pos = [PositionInfo(SYM, "short", 1.0, 100.0, 0.0)]
    sl = "OSKAR-r1-0123456789ab-sl"
    ex.open_list = [OpenOrderRef(SYM, "s1", sl, "sl")]
    ex.protection = [ProtectiveOrder("s1", "sl", 95.0, "open", client_order_id=sl, close_position=True)]
    _sided_protection(ex, {"s1": "long"})
    mon.tick()
    assert ex.cancelled == ["s1"] and ex.closed == []


def test_own_orphan_protection_of_untracked_position_kept_with_one_alert(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    register_run(j, "r1")
    ex.pos = [PositionInfo(SYM, "long", 1.0, 100.0, 0.0)]
    sl = "OSKAR-r1-0123456789ab-sl"
    ex.open_list = [OpenOrderRef(SYM, "s1", sl, "sl")]
    ex.protection = [ProtectiveOrder("s1", "sl", 95.0, "open", client_order_id=sl, close_position=True)]
    _sided_protection(ex, {"s1": "long"})
    mon.tick()
    assert ex.cancelled == []
    kept = [n for n in notes if "SL/TP OSKAR без сделки сохранен" in n]
    assert len(kept) == 1 and sl in kept[0] and "long" in kept[0]
    clock[0] += timedelta(minutes=11)
    mon.tick()
    assert ex.cancelled == [] and sum("SL/TP OSKAR без сделки сохранен" in n for n in notes) == 1
