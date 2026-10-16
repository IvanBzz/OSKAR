"""Ревью Codex (gpt-6-astra) релиза R1, блок H2 (2026-10-16): четыре P1.

1 — голый повтор входа шел после panic без проверки флагов; panic не ждал входов in-flight;
2 — R:R проверялся до долгой подготовки (плечо с повторами) и не по цене филла;
3 — защелка просадки читала часовой кеш equity runner'а;
4 — сбой записи SAFE MODE в meta снимал защиту при рестарте (второй носитель — маркер-файл).
"""

import logging
import sqlite3
import threading
import time

import pytest

from oskar.core import executor as executor_mod
from oskar.core.emergency import (
    engage_panic,
    engage_safe_mode,
    reset_panic,
    restore_safe_mode,
    retry_safe_mode_persist,
    safe_mode_engaged,
)
from oskar.core.executor import (
    KIND_FILL_OUT_OF_CAPS,
    KIND_OK,
    KIND_REFUSED,
    ExecutionOutcome,
    ExecutionService,
    equity_guarded,
)
from oskar.core.position_monitor import PositionMonitor
from oskar.core.state import RuntimeState
from oskar.core.trading_mode import AUTO, BREAKER_FAILURES, CONFIRM
from oskar.execution.base import OpenOrderRef, OrderState, PositionInfo, cid_base
from oskar.journal.db import connect
from oskar.risk import ladder
from oskar.risk.drawdown import DrawdownGuard, LiveEquity, read_halt, read_peak
from tests.unit.test_buttons import add_pending
from tests.unit.test_capital_protection import (
    _auto,
    _latched,
    _run,
    entries,
    h_plan,
    service,
    trades_count,
)
from tests.unit.test_equity_guard import make_runner
from tests.unit.test_position_monitor import SYM, make_monitor, trade
from tests.unit.test_protection_lifecycle import ExchangeError

REJECT = ExchangeError('bingx {"code":80001,"msg":"attached stopLoss not accepted"}')


# ============================================== P1-1: повторный вход после panic


def _engage_panic(state, j):
    state.panic()


def _engage_safe_mode(state, j):
    engage_safe_mode(RuntimeState(journal=j), "журнал и биржа расходятся")  # монитор, другой поток


def _engage_drawdown(state, j):
    _latched(j)


@pytest.mark.parametrize(("engage", "expected"), [
    (_engage_panic, "panic взведен — вход отменен"),
    (_engage_safe_mode, "SAFE MODE взведен — вход отменен"),
    (_engage_drawdown, "стоп по просадке: equity 945.00"),
])
def test_flag_raised_during_first_attempt_blocks_bare_retry(tmp_path, engage, expected):
    """Первый запрос (с SL/TP) в полете, другой поток взводит флаг; биржа отклоняет
    прикрепление — голый повтор НЕ уходит."""
    p = h_plan()
    svc, j, ex, state = service(tmp_path, p, price=[100.0])
    ex.reject_attached = REJECT
    orig = ex.place_entry

    def first_attempt_then_flag(*a, **k):
        engage(state, j)  # пока запрос у биржи
        return orig(*a, **k)

    ex.place_entry = first_attempt_then_flag
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and out.kind == KIND_REFUSED and not out.entry_sent
    assert expected in out.message
    assert len(entries(ex)) == 1 and entries(ex)[0][2] == p.sl  # только попытка с прикреплением
    assert trades_count(j) == 0


def test_bare_retry_still_goes_without_flags(tmp_path):
    p = h_plan()
    svc, j, ex, _ = service(tmp_path, p, price=[100.0])
    ex.reject_attached = REJECT
    out = svc.execute(p.candidate_id, "market")
    assert out.ok, out.message
    assert [e[2] for e in entries(ex)] == [p.sl, None]  # с прикреплением, затем голый


def test_panic_waits_for_inflight_entry_and_closes_it_in_final_pass(tmp_path):
    """Вход прошел последнюю проверку и висит у биржи; panic flatten его не видит (записи
    нет) — ждет опустения реестра in-flight и закрывает только что записанную сделку."""
    p = h_plan()
    svc, j, ex, state = service(tmp_path, p, price=[100.0])
    notes = []
    mon = PositionMonitor(ex, j, max_loss_hold_seconds=3600, limit_entry_ttl_seconds=3600,
                          notify=notes.append, state=state)
    sent, release = threading.Event(), threading.Event()
    orig = ex.place_entry

    def slow_entry(*a, **k):
        sent.set()
        release.wait(5)
        return orig(*a, **k)

    ex.place_entry = slow_entry
    result = {}
    worker = threading.Thread(target=lambda: result.update(out=svc.execute(p.candidate_id, "market")))
    worker.start()
    assert sent.wait(5)
    key = cid_base(executor_mod.make_client_order_id("r1", p.candidate_id))
    assert executor_mod.inflight_entries() == {key: (p.symbol, p.side)}
    timer = threading.Timer(0.3, release.set)
    timer.start()
    out = engage_panic(state, mon, inflight_wait_sec=5.0)
    worker.join(5)
    assert result["out"].ok  # вход ушел до panic
    row = j.execute("SELECT status, close_reason FROM trades WHERE decision_id=?",
                    (p.candidate_id,)).fetchone()
    assert (row["status"], row["close_reason"]) == ("closed", "panic")
    assert ("close", p.symbol, p.side) in ex.calls
    assert out["exchange"]["closed"] == 1 and "inflight_unfinished" not in out["exchange"]
    assert executor_mod.inflight_entries() == {}


def test_panic_without_inflight_makes_a_single_pass(tmp_path):
    calls = []

    class Monitor:
        def panic_flatten(self):
            calls.append(1)
            return {"cancelled": 0, "closed": 0, "errors": []}

    out = engage_panic(RuntimeState(journal=connect(tmp_path / "j.db")), Monitor())
    assert calls == [1] and out["exchange"] == {"cancelled": 0, "closed": 0, "errors": []}


def test_panic_flattens_by_exchange_when_inflight_does_not_finish(tmp_path):
    """Вход так и не записан за окно: алерт и снятие по бирже по clientOrderId входа и его
    символу/стороне. Чужие ордера (другой cid, ручные без cid) не трогаем."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    cid = executor_mod.make_client_order_id("r9", "SOLUSDTUSDT-7-long")
    key = cid_base(cid)
    ex.open_list = [
        OpenOrderRef(SYM, "e1", cid, "entry"),
        OpenOrderRef(SYM, "s1", cid + "-sl", "sl"),
        OpenOrderRef(SYM, "x1", "OSKAR-r9-0123456789ab", "entry"),
        OpenOrderRef(SYM, "m1", "", "sl"),
    ]
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 0.0)]
    t = [0.0]
    with executor_mod._INFLIGHT_LOCK:
        executor_mod._INFLIGHT.add(key)
        executor_mod._INFLIGHT_TARGETS[key] = (SYM, "long")
    try:
        out = engage_panic(state, mon, inflight_wait_sec=2.0,
                           sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    finally:
        with executor_mod._INFLIGHT_LOCK:
            executor_mod._INFLIGHT.discard(key)
            executor_mod._INFLIGHT_TARGETS.pop(key, None)
    assert t[0] >= 2.0  # ждали окно целиком
    assert sorted(ex.cancelled) == ["e1", "s1"]
    assert ex.closed == [(SYM, "long")]
    assert out["exchange"]["inflight_unfinished"] == [key]
    assert out["exchange"]["cancelled"] == 2 and out["exchange"]["closed"] == 1
    assert any("не записан за окно ожидания" in n for n in notes)


# ============================================ P1-2: R:R перед отправкой и по филлу


def test_price_rechecked_after_slow_leverage(tmp_path):
    """Цена при нажатии 100 (R:R 3), за время установки плеча с повторами ушла на 100.49:
    вход по старой проверке не отправляется."""
    p = h_plan()
    px = [100.0]
    svc, j, ex, _ = service(tmp_path, p, price=px)
    orig = ex.set_leverage

    def slow_leverage(*a):
        orig(*a)
        px[0] = 100.49

    ex.set_leverage = slow_leverage
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and out.kind == KIND_REFUSED and not out.entry_sent
    assert "R:R по текущей цене 1.68 < 2.0 — вход отменен" in out.message
    assert ("leverage", 3.0) in ex.calls and entries(ex) == [] and trades_count(j) == 0


def test_bare_retry_rechecks_price(tmp_path):
    p = h_plan()
    px = [100.0]
    svc, j, ex, _ = service(tmp_path, p, price=px)
    ex.reject_attached = REJECT
    orig = ex.place_entry

    def first_attempt_moves_price(*a, **k):
        px[0] = 100.49
        return orig(*a, **k)

    ex.place_entry = first_attempt_moves_price
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and "R:R по текущей цене 1.68" in out.message and not out.entry_sent
    assert len(entries(ex)) == 1


def _spy_qty(ex) -> list:
    sent = []
    orig = ex.place_entry

    def spy(symbol, side, qty, cid, **kw):
        sent.append(qty)
        return orig(symbol, side, qty, cid, **kw)

    ex.place_entry = spy
    return sent


@pytest.mark.parametrize(("px_at_send", "sized_at"), [
    (100.3, 100.3),  # цена ушла от стопа: потеря на стопе больше — qty вниз
    (99.9, 100.0),   # цена ближе к стопу: qty НЕ растет (только вниз)
])
def test_qty_resized_down_only_before_send(tmp_path, px_at_send, sized_at):
    p = h_plan(tp=104.0)
    px = [100.0]
    svc, j, ex, _ = service(tmp_path, p, price=px)
    orig = ex.set_leverage
    ex.set_leverage = lambda *a: (orig(*a), px.__setitem__(0, px_at_send))
    sent = _spy_qty(ex)
    out = svc.execute(p.candidate_id, "market")
    assert out.ok, out.message
    expected = round(ExecutionService.risk_sized_qty(p, sized_at), 4)
    assert sent == [pytest.approx(expected)]
    assert trade(j, p.candidate_id)["qty"] == pytest.approx(expected)
    if px_at_send == 100.3:
        assert expected < round(ExecutionService.risk_sized_qty(p, 100.0), 4)


def _fill_at(ex, fill_px):
    orig = ex.place_entry

    def entry(symbol, side, qty, cid, **kw):
        order = orig(symbol, side, qty, cid, **kw)
        ex.orders[order.exchange_id] = OrderState(order.exchange_id, "filled", qty, fill_px, 0.05)
        return order

    ex.place_entry = entry


def test_fill_out_of_caps_is_alarm_not_auto_close(tmp_path):
    """Перед отправкой 100 (R:R 3), филл 100.49 (R:R 1.68 < 2.0 - люфт): позиция с SL/TP
    остается, алерт, журнал, исход fill_out_of_caps (его считает предохранитель)."""
    p = h_plan()
    svc, j, ex, _ = service(tmp_path, p, price=[100.0])
    notes = []
    svc._notify = notes.append
    _fill_at(ex, 100.49)
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and out.entry_sent and out.kind == KIND_FILL_OUT_OF_CAPS
    assert ("вход исполнен по 100.49, R:R по филлу 1.68 < капа 2.0 — позиция защищена SL/TP, "
            "проверь") in out.message
    assert notes and "R:R по филлу 1.68" in notes[0]
    assert not any(c[0] == "close" for c in ex.calls)  # не закрываем
    row = trade(j, p.candidate_id)
    assert row["status"] == "open" and row["entry_price"] == 100.49
    detail = j.execute("SELECT outcome_detail FROM decisions WHERE decision_id=?",
                       (p.candidate_id,)).fetchone()[0]
    assert "fill_out_of_caps: R:R по филлу 1.68" in detail


def test_fill_within_allowance_is_ok(tmp_path):
    p = h_plan()
    svc, j, ex, _ = service(tmp_path, p, price=[100.0])
    _fill_at(ex, 100.36)  # R:R по филлу 1.94: ниже капа, но в люфте INV-004
    out = svc.execute(p.candidate_id, "market")
    assert out.ok and out.kind == KIND_OK, out.message


def test_fill_stop_distance_out_of_profile(tmp_path):
    p = h_plan(sl=97.6, tp=110.0)  # стоп 2.4% от 100; R:R по филлу ок
    svc, j, ex, _ = service(tmp_path, p, price=[100.0], max_drift_pct=1.0)
    _fill_at(ex, 100.8)  # стоп по филлу 3.17% > 3.0 + 0.05
    out = svc.execute(p.candidate_id, "market")
    assert out.kind == KIND_FILL_OUT_OF_CAPS and "стоп по филлу 3.17% вне [0.5..3.0]%" in out.message


def test_breaker_counts_fill_out_of_caps_in_series(tmp_path):
    bad = ExecutionOutcome(False, "вход исполнен по 100.49, R:R по филлу 1.68 < капа 2.0",
                           entry_sent=True, kind=KIND_FILL_OUT_OF_CAPS)
    auto, state, mode = _auto(tmp_path, [bad] * BREAKER_FAILURES)
    results = _run(auto, state, BREAKER_FAILURES - 1)
    assert all(r.executed for r in results) and mode.get() == AUTO  # не размыкает с первого раза
    add_pending(state.journal, "last")
    assert auto.run("last", "market").executed  # вход был — approve не откатываем
    assert mode.get() == CONFIRM


def test_fill_allowance_is_shared_with_invariant():
    from oskar.ops import invariants

    assert invariants.FILL_RR_ALLOWANCE is ladder.FILL_RR_ALLOWANCE
    assert invariants.FILL_SL_PCT_ALLOWANCE is ladder.FILL_SL_PCT_ALLOWANCE


# ======================================== P1-3: свежий equity для защелки просадки


def test_drawdown_latches_within_one_monitor_tick_between_hourly_updates(tmp_path):
    """Runner обновил equity=1000 и до следующего часа видит его же; баланс падает до 890 —
    монитор ставит защелку на ближайшем полном тике своим запросом к бирже."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    balance = [1000.0]
    live = LiveEquity(lambda: balance[0])
    runner, _, t = make_runner(tmp_path, equity_fn=live.refresh, journal=j)
    runner.tick()
    assert runner.equity_usd == 1000.0 and runner.equity_source == "exchange"
    mon._drawdown = DrawdownGuard(j, 10, equity_fn=lambda: runner.equity_usd,
                                  equity_source_fn=lambda: runner.equity_source, live=live)
    mon.tick()
    assert read_peak(j) == 1000.0 and read_halt(j) is None
    balance[0] = 890.0
    t[0] = 60.0
    runner.tick()  # минута: часовой кеш runner'а все еще 1000 с источником exchange
    assert runner.equity_usd == 1000.0 and runner.equity_source == "exchange"
    mon.tick()
    halt = read_halt(j)
    assert halt is not None and halt.equity == 890.0 and halt.peak == 1000.0
    assert sum("стоп по просадке" in n for n in notes) == 1


def test_monitor_equity_failure_is_logged_and_tick_survives(tmp_path, caplog):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)

    def boom():
        raise ConnectionError("bingx balance timeout")

    mon._drawdown = DrawdownGuard(j, 10, live=LiveEquity(boom))
    with caplog.at_level(logging.WARNING, logger="oskar.drawdown"):
        rep = mon.tick()
    assert mon.ticks == 1 and not any("drawdown" in e for e in rep.errors)
    assert read_halt(j) is None and read_peak(j) is None
    assert any("equity fetch failed" in r.getMessage() for r in caplog.records)


def test_live_equity_times_out_and_does_not_pile_up_threads():
    gate = threading.Event()
    calls = []

    def hanging():
        calls.append(1)
        gate.wait(5)
        return 1000.0

    live = LiveEquity(hanging, timeout_sec=0.05)
    started = time.monotonic()
    assert live.refresh() is None and time.monotonic() - started < 2.0
    assert live.refresh() is None and len(calls) == 1  # прежний запрос висит — новый не шлем
    gate.set()
    live._worker.join(2)
    assert live.refresh() == 1000.0 and len(calls) == 2


def test_executor_takes_fresh_equity_or_refuses():
    t = [0.0]
    fetches = []
    value = [1000.0]

    def fetch():
        fetches.append(t[0])
        return value[0]

    live = LiveEquity(fetch, clock=lambda: t[0])
    executed = []
    guarded = equity_guarded(
        lambda d, m: executed.append(d) or ExecutionOutcome(True, "ok"),
        lambda: "persisted",  # часовой источник runner'а не решает: решает свежий equity
        live=live,
    )
    assert guarded("d1", "market").ok and fetches == [0.0]  # значения не было — запрос
    t[0] = 100.0
    assert guarded("d2", "market").ok and fetches == [0.0]  # 100 с — свежее, без запроса
    t[0] = 221.0
    value[0] = None  # биржа молчит
    out = guarded("d3", "market")
    assert not out.ok and out.kind == KIND_REFUSED and "нет свежего equity с биржи" in out.message
    assert fetches == [0.0, 221.0] and executed == ["d1", "d2"]


def test_executor_latches_drawdown_on_fresh_equity_before_entry(tmp_path):
    j = connect(tmp_path / "j.db")
    value = [1000.0]
    t = [0.0]
    live = LiveEquity(lambda: value[0], clock=lambda: t[0])
    dd = DrawdownGuard(j, 10, live=live)
    assert dd.check() is None and read_peak(j) == 1000.0
    executed = []
    guarded = equity_guarded(lambda d, m: executed.append(d) or ExecutionOutcome(True, "ok"),
                             lambda: "exchange", live=live, drawdown=dd)
    value[0] = 890.0
    t[0] = 121.0
    out = guarded("d1", "market")
    assert not out.ok and out.kind == KIND_REFUSED and executed == []
    assert "стоп по просадке: equity 890.00 от пика 1000.00" in out.message
    assert read_halt(j) is not None


# =================================================== P1-4: SAFE MODE на двух носителях


class FlakyJournal:
    """Журнал, у которого запись падает «database is locked», пока locked=True (чтение идет)."""

    def __init__(self, conn):
        self._c = conn
        self.locked = True

    def execute(self, sql, *a):
        if self.locked and not sql.lstrip().upper().startswith("SELECT"):
            raise sqlite3.OperationalError("database is locked")
        return self._c.execute(sql, *a)

    def commit(self):
        self._c.commit()

    def rollback(self):
        self._c.rollback()


def test_meta_failure_flag_file_restores_safe_mode_on_new_start(tmp_path):
    j = connect(tmp_path / "j.db")
    flag = tmp_path / "data" / "safe_mode.flag"
    state = RuntimeState(journal=FlakyJournal(j), safe_mode_flag=flag)
    assert engage_safe_mode(state, "позиция SOL/USDT:USDT без защиты")
    assert state.safe_mode and state.safe_mode_unsaved
    assert safe_mode_engaged(j) is None  # meta не записана
    assert flag.exists() and not flag.with_name(flag.name + ".tmp").exists()  # tmp + rename
    # демон остановлен с несохраненной meta — новый старт видит SAFE MODE по маркер-файлу
    restarted = RuntimeState(journal=j, safe_mode_flag=flag)
    alerts = []
    assert restore_safe_mode(restarted, notify=alerts.append) == "позиция SOL/USDT:USDT без защиты"
    assert restarted.safe_mode and "SAFE MODE восстановлен после рестарта" in alerts[0]
    assert safe_mode_engaged(j)["reason"] == "позиция SOL/USDT:USDT без защиты"  # дописан


def test_monitor_retries_meta_write_until_db_recovers(tmp_path):
    mon, j, ex, clock, notes, _, _ = make_monitor(tmp_path)
    flaky = FlakyJournal(j)
    state = RuntimeState(journal=flaky, safe_mode_flag=tmp_path / "safe_mode.flag")
    mon._state = state
    mon._set_safe_mode("журнал и биржа расходятся")
    assert state.safe_mode and state.safe_mode_unsaved and safe_mode_engaged(j) is None
    mon.tick()  # БД все еще занята
    assert state.safe_mode_unsaved and safe_mode_engaged(j) is None
    flaky.locked = False
    mon.tick()
    assert not state.safe_mode_unsaved
    assert safe_mode_engaged(j)["reason"] == "журнал и биржа расходятся"


def test_runner_retries_meta_write_until_db_recovers(tmp_path):
    runner, state, t = make_runner(tmp_path, equity_fn=None)
    j = state.journal
    flaky = FlakyJournal(j)
    state.journal = flaky
    state.safe_mode_flag = tmp_path / "safe_mode.flag"
    runner._enter_safe_mode("3 consecutive shadow failures")
    assert state.safe_mode_unsaved and state.safe_mode_flag.exists()
    runner.tick()
    assert state.safe_mode_unsaved
    flaky.locked = False
    runner.tick()
    assert not state.safe_mode_unsaved and safe_mode_engaged(j) is not None


def test_panic_reset_lifts_both_carriers_and_retry_does_not_resurrect(tmp_path):
    j = connect(tmp_path / "j.db")
    flaky = FlakyJournal(j)
    flag = tmp_path / "safe_mode.flag"
    state = RuntimeState(journal=flaky, safe_mode_flag=flag)
    engage_safe_mode(state, "x")
    assert state.safe_mode_unsaved and flag.exists()
    flaky.locked = False
    reset_panic(state)
    assert not state.safe_mode and not state.safe_mode_unsaved and not flag.exists()
    assert retry_safe_mode_persist(state) is None and safe_mode_engaged(j) is None
    again = RuntimeState(journal=j, safe_mode_flag=flag)
    assert restore_safe_mode(again) is None and not again.safe_mode

    engage_safe_mode(again, "y")  # запись удалась: оба носителя
    assert flag.exists() and safe_mode_engaged(j) is not None
    reset_panic(again)
    assert not flag.exists() and safe_mode_engaged(j) is None


def test_panic_not_persisted_falls_back_to_safe_mode_carriers(tmp_path):
    j = connect(tmp_path / "j.db")
    flaky = FlakyJournal(j)
    flag = tmp_path / "safe_mode.flag"
    state = RuntimeState(journal=flaky, safe_mode_flag=flag)
    out = engage_panic(state)
    assert "locked" in out["persist_error"] and flag.exists() and state.safe_mode_unsaved
    flaky.locked = False
    assert retry_safe_mode_persist(state) is True
    assert "panic не записан в журнал" in safe_mode_engaged(j)["reason"]
    restarted = RuntimeState(journal=j, safe_mode_flag=flag)
    assert restore_safe_mode(restarted) is not None and restarted.safe_mode


def test_meta_only_safe_mode_gets_flag_file_on_start(tmp_path):
    """SAFE MODE, взведенный до R1 (только meta): старт дописывает маркер-файл."""
    j = connect(tmp_path / "j.db")
    engage_safe_mode(RuntimeState(journal=j), "x")
    flag = tmp_path / "safe_mode.flag"
    st = RuntimeState(journal=j, safe_mode_flag=flag)
    assert restore_safe_mode(st) == "x" and flag.exists() and not st.safe_mode_unsaved


def test_unreadable_flag_file_still_means_safe_mode(tmp_path):
    j = connect(tmp_path / "j.db")
    flag = tmp_path / "safe_mode.flag"
    flag.write_text("{битый", encoding="utf-8")
    st = RuntimeState(journal=j, safe_mode_flag=flag)
    assert restore_safe_mode(st) == "{битый" and st.safe_mode
