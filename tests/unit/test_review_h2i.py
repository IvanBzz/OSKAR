"""Ревью Codex (gpt-6-astra) выкаченных блоков H2 и I (2026-10-16): шесть P1 и один P2.

1 — флаг защиты, взведенный во время сетевой проверки цены перед отправкой, не останавливал вход;
2 — panic пропускал финальный проход и терял вход, принятый биржей после окна ожидания;
3 — старт снимал единственный SL потерянной сделки до сверки позиций;
4 — параллельные входы РАЗНЫХ решений обходили лимит позиций и запрет дубля symbol/side;
5 — дедуп алерта о чужих ордерах отключал повторный взвод SAFE MODE после panic-reset;
6 — аварийное закрытие in-flight слало Telegram до биржевых действий и под замком flatten;
7 — сбой записи отметки digest вызывал повторную отправку сводки каждые 30 с.
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from oskar.core import emergency as emergency_mod
from oskar.core.emergency import engage_panic, reset_panic, safe_mode_engaged
from oskar.core.executor import KIND_REFUSED, make_client_order_id
from oskar.core.ops_jobs import DIGEST_SENT_KEY, OpsScheduler, ops_config
from oskar.core.position_monitor import ORPHAN_SCAN_SEC, PositionMonitor, _iso
from oskar.core.run_registry import register_run
from oskar.execution.base import (
    OpenOrderRef,
    OrderState,
    PositionInfo,
    ProtectiveOrder,
    cid_base,
)
from oskar.journal.db import connect
from tests.unit.test_buttons import add_pending
from tests.unit.test_capital_protection import entries, h_plan, service, trades_count
from tests.unit.test_ops_digest import NOW, Sink
from tests.unit.test_position_monitor import SYM, T0, add_trade, make_monitor, trade
from tests.unit.test_review_r1 import (
    FlakyJournal,
    _engage_drawdown,
    _engage_panic,
    _engage_safe_mode,
)
from tests.unit.test_second_machine import FOREIGN


def _fake_time():
    t = [0.0]
    return t, (lambda s: t.__setitem__(0, t[0] + s)), (lambda: t[0])


def _blocked(ex, name):
    """Метод биржи ждет release: первый вызов сообщает started и висит (сеть)."""
    started, release = threading.Event(), threading.Event()
    orig = getattr(ex, name)
    calls = []

    def slow(*a, **k):
        calls.append(a)
        if len(calls) == 1:
            started.set()
            release.wait(5)
        return orig(*a, **k)

    setattr(ex, name, slow)
    return started, release


def _in_thread(fn):
    box = {}
    worker = threading.Thread(target=lambda: box.update(out=fn()))
    worker.start()
    return worker, box


def _add_decision(j, p, *, resolution=None, run="r1"):
    add_pending(j, p.candidate_id)
    j.execute("UPDATE pending_approvals SET card_json=?, resolution=?, resolved_at=? WHERE decision_id=?",
              (p.model_dump_json(), resolution, _iso(datetime.now(UTC)) if resolution else None,
               p.candidate_id))
    j.execute("INSERT INTO decisions (run_id, decision_id, candidate_json, outcome) "
              "VALUES (?, ?, '{}', 'pending')", (run, p.candidate_id))
    j.commit()


# ================================= P1-1: флаг, взведенный во время сети перед отправкой


@pytest.mark.parametrize(("engage", "expected"), [
    (_engage_panic, "panic взведен — вход отменен"),
    (_engage_safe_mode, "SAFE MODE взведен — вход отменен"),
    (_engage_drawdown, "стоп по просадке: equity 945.00"),
])
def test_flag_raised_during_send_gate_price_check_blocks_entry(tmp_path, engage, expected):
    """Тикер в последнем рубеже отвечает, а в это время монитор взводит SAFE MODE/просадку или
    оператор — panic: допуск к отправке перепроверяет флаги ПОСЛЕ сети."""
    p = h_plan()
    svc, j, ex, state = service(tmp_path, p, price=[100.0])
    calls = []

    def price(symbol):
        calls.append(symbol)
        if len(calls) == 2:  # первая цена — при нажатии, вторая — в последнем рубеже
            engage(state, j)
        return 100.0

    svc._price_fn = price
    out = svc.execute(p.candidate_id, "market")
    assert len(calls) == 2
    assert not out.ok and out.kind == KIND_REFUSED and not out.entry_sent
    assert expected in out.message
    assert entries(ex) == [] and trades_count(j) == 0


def test_panic_does_not_wait_for_entry_before_admission_and_it_is_refused(tmp_path):
    """Допуск к отправке и взвод panic — одна короткая критическая секция: вход, еще не
    допущенный (плечо в сети), на биржу уже не уйдет — panic его не ждет."""
    p = h_plan()
    svc, j, ex, state = service(tmp_path, p, price=[100.0])
    mon = PositionMonitor(ex, j, max_loss_hold_seconds=3600, limit_entry_ttl_seconds=3600,
                          state=state)
    started, release = _blocked(ex, "set_leverage")
    worker, box = _in_thread(lambda: svc.execute(p.candidate_id, "market"))
    try:
        assert started.wait(5)
        t, sleep, clock = _fake_time()
        out = engage_panic(state, mon, inflight_wait_sec=5.0, sleep=sleep, clock=clock)
        assert t[0] == 0.0  # не ждал окно
        assert "inflight_unfinished" not in out["exchange"]
    finally:
        release.set()
        worker.join(5)
    assert not box["out"].ok and "panic взведен" in box["out"].message
    assert entries(ex) == [] and trades_count(j) == 0


# ===================================================== P1-2: panic и поздние входы


def test_panic_final_pass_runs_even_if_entry_left_registry_before_wait(tmp_path):
    """Первый проход прочитал пустой список сделок; исполнитель тут же записал сделку и вышел
    из реестра in-flight — ожидание видит пустой реестр. Финальный проход — всегда."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    orig = mon.panic_flatten
    passes = []

    def flatten_then_entry_recorded():
        out = orig()
        passes.append(out)
        if len(passes) == 1:  # запись сделки после чтения списка первым проходом
            add_trade(j, "late")
            ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 0.0)]
        return out

    mon.panic_flatten = flatten_then_entry_recorded
    out = engage_panic(state, mon)
    assert len(passes) == 2
    row = trade(j, "late")
    assert (row["status"], row["close_reason"]) == ("closed", "panic")
    assert ex.closed == [(SYM, "long")] and out["exchange"]["closed"] == 1


def test_entry_accepted_after_panic_window_is_chased_until_flat(tmp_path):
    """Вход висит у биржи дольше окна ожидания; аварийный проход по бирже его не застал. Биржа
    приняла вход позже, исполнитель записал открытую сделку — монитор закрывает ее по panic и
    следит, пока отсутствие экспозиции не подтверждено."""
    p = h_plan()
    svc, j, ex, state = service(tmp_path, p, price=[100.0])
    now = [T0]
    mon = PositionMonitor(ex, j, max_loss_hold_seconds=3600, limit_entry_ttl_seconds=3600,
                          state=state, now_fn=lambda: now[0])
    started, release = _blocked(ex, "place_entry")
    worker, box = _in_thread(lambda: svc.execute(p.candidate_id, "market"))
    key = cid_base(make_client_order_id("r1", p.candidate_id))
    try:
        assert started.wait(5)
        _, sleep, clock = _fake_time()
        out = engage_panic(state, mon, inflight_wait_sec=1.0, sleep=sleep, clock=clock)
        assert out["exchange"]["inflight_unfinished"] == [key]
    finally:
        release.set()  # биржа приняла вход уже после окна и аварийного прохода
        worker.join(5)
    assert box["out"].ok and trade(j, p.candidate_id)["status"] == "open"
    closes = sum(c[0] == "close" for c in ex.calls)
    mon.tick_pending()
    row = trade(j, p.candidate_id)
    assert (row["status"], row["close_reason"]) == ("closed", "panic")
    assert sum(c[0] == "close" for c in ex.calls) == closes + 1 and ex.pos == []
    assert mon.chasing() == [key]  # отсутствие еще не подтверждено
    for _ in range(3):
        now[0] += timedelta(minutes=5)
        mon.tick_pending()
    assert mon.chasing() == []


def test_panic_chase_stops_after_operator_reset(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    state.panic()
    mon.chase_after_panic({"oskar-r9-0123456789ab": (SYM, "long")})
    reset_panic(state)
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 0.0)]  # после снятия — обычная позиция
    mon.tick_pending()
    assert mon.chasing() == [] and ex.closed == []


# ======================================== P1-3: старт не снимает SL потерянной сделки


def test_startup_restores_lost_trade_and_keeps_its_only_protection(tmp_path):
    """Процесс упал после маркета и отдельных SL/TP, но до записи сделки (решение approved,
    запуск зарегистрирован). Раньше старт снимал эти SL/TP как «своих сирот» ДО сверки позиций,
    и позиция оставалась без стопа и без сделки."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    register_run(j, "r1")
    p = h_plan()
    _add_decision(j, p, resolution="approved")
    cid = make_client_order_id("r1", p.candidate_id)
    ex.pos = [PositionInfo(p.symbol, "long", 3.0, 100.1, 0.0)]
    ex.open_list = [OpenOrderRef(p.symbol, "s1", cid + "-sl", "sl"),
                    OpenOrderRef(p.symbol, "t1", cid + "-tp", "tp")]
    ex.protection = [ProtectiveOrder("s1", "sl", p.sl, "open", client_order_id=cid + "-sl",
                                     close_position=True),
                     ProtectiveOrder("t1", "tp", p.tp, "open", client_order_id=cid + "-tp",
                                     close_position=True)]
    ex.orders.update({"s1": OrderState("s1", "open"), "t1": OrderState("t1", "open")})
    mon.startup_check()
    assert ex.cancelled == [] and ex.closed == []  # единственный SL на месте
    row = trade(j, p.candidate_id)
    assert row is not None and row["status"] == "open" and row["client_order_id"] == cid
    assert (row["qty"], row["entry_price"], row["sl_price"], row["tp_price"]) == (3.0, 100.1, p.sl, p.tp)
    assert (row["sl_order_id"], row["tp_order_id"]) == ("s1", "t1")  # защита усыновлена
    outcome = j.execute("SELECT outcome FROM decisions WHERE decision_id=?", (p.candidate_id,)).fetchone()
    assert outcome[0] == "executed"
    assert state.safe_mode and "учет восстановлен" in safe_mode_engaged(j)["reason"]
    assert any("учет восстановлен" in n for n in notes)
    clock[0] += timedelta(seconds=61)
    mon.tick()  # сопровождение как у обычной сделки: без снятий и расхождений
    assert ex.cancelled == [] and trade(j, p.candidate_id)["status"] == "open"


def test_startup_untracked_position_without_decision_keeps_own_protection(tmp_path):
    """Учет не восстановить (решения нет) — позицию не закрываем, SAFE MODE, а свой SL/TP на ее
    символе не снимаем; сирота на символе без позиции снимается как раньше."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    register_run(j, "r1")
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 0.0)]
    ex.open_list = [OpenOrderRef(SYM, "s1", "OSKAR-r1-0123456789ab-sl", "sl"),
                    OpenOrderRef("A/USDT:USDT", "o1", "OSKAR-r1-aaaaaaaaaaaa-sl", "sl")]
    # s1 — защита ИМЕННО этой позиции (protection_orders по symbol/side; ревью H3, 2-й круг)
    ex.protection = [ProtectiveOrder("s1", "sl", 98.0, "open", client_order_id="OSKAR-r1-0123456789ab-sl",
                                     close_position=True)]
    mon.startup_check()
    assert ex.cancelled == ["o1"] and ex.closed == []
    assert state.safe_mode and trades_count(j) == 0


def test_startup_does_not_restore_ambiguous_decisions(tmp_path):
    """Два approved-решения без сделки по тому же symbol/side — чья позиция, неизвестно."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    register_run(j, "r1")
    for i in (1, 2):
        _add_decision(j, h_plan(candidate_id=f"SOLUSDTUSDT-{i}-long"), resolution="approved")
    ex.pos = [PositionInfo(SYM, "long", 3.0, 100.0, 0.0)]
    mon.startup_check()
    assert trades_count(j) == 0 and state.safe_mode and ex.closed == []


def test_orphan_protection_kept_while_positions_unknown(tmp_path):
    """Позиции не прочитаны — отсутствие позиции не подтверждено: свой SL/TP-сирота остается,
    вход-сирота снимается (он мог бы открыть позицию мимо журнала)."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    register_run(j, "r1")
    ex.open_list = [OpenOrderRef(SYM, "s1", "OSKAR-r1-0123456789ab-sl", "sl"),
                    OpenOrderRef(SYM, "e1", "OSKAR-r1-bbbbbbbbbbbb", "entry")]

    def down():
        raise ConnectionError("positions timeout")

    ex.positions = down
    mon.tick()
    assert ex.cancelled == ["e1"]


# ========================= P1-4: резервация слота и symbol/side для разных решений


@pytest.mark.parametrize(("second", "max_open", "expected"), [
    (dict(candidate_id="SOLUSDTUSDT-2-long"), None, "SOL/USDT:USDT long"),  # дубль symbol/side
    (dict(candidate_id="ETHUSDTUSDT-2-long", symbol="ETH/USDT:USDT"), 1,
     "лимит открытых позиций: 1/1"),
])
def test_concurrent_entries_of_different_decisions_reserve_slot(tmp_path, second, max_open, expected):
    """Вход A прошел проверки при нажатии и ставит плечо (сеть); в это время API/автомат
    исполняет решение B — журнал еще пуст, но слот и symbol/side уже заняты резервацией A."""
    p1 = h_plan()
    svc, j, ex, state = service(tmp_path, p1, price=[100.0])
    svc._max_open = max_open
    p2 = h_plan(**second)
    _add_decision(j, p2)
    started, release = _blocked(ex, "set_leverage")
    worker, box = _in_thread(lambda: svc.execute(p1.candidate_id, "market"))
    try:
        assert started.wait(5)
        out_b = svc.execute(p2.candidate_id, "market")
    finally:
        release.set()
        worker.join(5)
    assert not out_b.ok and out_b.kind == KIND_REFUSED and not out_b.entry_sent
    assert expected in out_b.message
    assert box["out"].ok
    assert len(entries(ex)) == 1 and trades_count(j) == 1
    # резервация снята вместе с реестром: после записи A лимит считает журнал
    from oskar.core import executor as executor_mod

    assert executor_mod.reserved_entries() == {}


# ===================================== P1-5: чужие ордера — SAFE MODE всегда, дедуп — алерт


def test_foreign_orders_reengage_safe_mode_after_operator_reset(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    ex.open_list = [OpenOrderRef(SYM, "f1", FOREIGN + "-sl", "sl")]
    mon.tick()
    assert state.safe_mode
    reset_panic(state)  # оператор снял SAFE MODE, а ордера второй машины остались
    assert not state.safe_mode and safe_mode_engaged(j) is None
    clock[0] += timedelta(seconds=ORPHAN_SCAN_SEC + 1)
    mon.tick()
    assert state.safe_mode and safe_mode_engaged(j) is not None and ex.cancelled == []
    assert sum("чужого запуска" in n for n in notes) == 2  # повторный взвод — с объяснением
    clock[0] += timedelta(seconds=ORPHAN_SCAN_SEC + 1)
    mon.tick()
    assert sum("чужого запуска" in n for n in notes) == 2  # защелка стоит — без повтора алерта


# ============================ P1-6: panic — биржа первой, Telegram после и вне замка


def test_panic_sends_telegram_after_exchange_actions_and_outside_flatten_lock(tmp_path):
    p = h_plan()
    svc, j, ex, state = service(tmp_path, p, price=[100.0])
    events = []

    def notify(text):
        events.append((emergency_mod._FLATTEN_LOCK.locked(),
                       sum(c[0] == "close" for c in ex.calls), text))

    mon = PositionMonitor(ex, j, max_loss_hold_seconds=3600, limit_entry_ttl_seconds=3600,
                          state=state, notify=notify)
    add_trade(j, "open1", symbol="B/USDT:USDT")
    started, release = _blocked(ex, "place_entry")
    worker, box = _in_thread(lambda: svc.execute(p.candidate_id, "market"))
    try:
        assert started.wait(5)
        _, sleep, clock = _fake_time()
        engage_panic(state, mon, inflight_wait_sec=1.0, sleep=sleep, clock=clock)
        assert events, "алерт panic не ушел"
        assert not any(locked for locked, _, _ in events)  # Telegram — не под замком flatten
        assert events[0][1] == 2  # сделка журнала и вход in-flight закрыты ДО первого алерта
        assert any("не записан за окно ожидания" in text for _, _, text in events)
    finally:
        release.set()
        worker.join(5)


# =========================================== P2-7: digest — отметка отправки в памяти


def test_digest_not_resent_when_sent_mark_fails_to_persist(tmp_path):
    j = connect(tmp_path / "j.db")
    flaky = FlakyJournal(j)  # запись meta падает (внешний write-lock SQLite), чтение идет
    sink = Sink()
    ops = OpsScheduler(flaky, ops_config({}), watch=None, digest_fn=lambda: "сводка", notify=sink,
                       started_at=NOW - timedelta(hours=3))
    ops.tick(NOW)
    ops.tick(NOW + timedelta(seconds=30))
    ops.tick(NOW + timedelta(seconds=60))
    assert sink.texts == ["сводка"]  # доставлено — повторяется только запись отметки
    assert j.execute("SELECT value FROM meta WHERE key=?", (DIGEST_SENT_KEY,)).fetchone() is None
    flaky.locked = False
    ops.tick(NOW + timedelta(seconds=90))
    assert j.execute("SELECT value FROM meta WHERE key=?", (DIGEST_SENT_KEY,)).fetchone()[0] == "2026-10-16"
    assert sink.texts == ["сводка"]
