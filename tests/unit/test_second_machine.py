"""Блок I1 (2026-10-16): вторая машина и «сироты».

Скан сирот снимал любой ордер с cid «OSKAR-», которого нет в журнале. Второй oskard на
ноутбуке (свой журнал) снял бы SL/TP первого. Теперь: run_id из cid известен журналу —
свой сирота, снимаем; неизвестен — чужой запуск: не трогаем, SAFE MODE + алерт. То же при
старте демона до первого входа; позиция без сделки при старте — SAFE MODE, не закрываем.
"""

from datetime import timedelta

from oskar.core.emergency import safe_mode_engaged
from oskar.core.position_monitor import ORPHAN_SCAN_SEC
from oskar.core.run_registry import cid_run_id, register_run, run_known
from oskar.execution.base import OpenOrderRef, OrderState, PositionInfo
from tests.unit.test_position_monitor import SYM, T0, add_trade, make_monitor

FOREIGN = "OSKAR-ffff0000-0123456789ab"  # run_id, которого этот журнал не видел


def test_cid_run_id_parses_oskar_ids_only():
    assert cid_run_id("OSKAR-ab12cd34-0123456789ab") == "ab12cd34"
    assert cid_run_id("oskar-AB12CD34-0123456789ab-sl-r2") == "ab12cd34"
    assert cid_run_id("OSKAR-") is None and cid_run_id("manual-ivan") is None
    assert cid_run_id("OSKAR-abc") is None and cid_run_id("") is None


def test_run_known_by_meta_trades_decisions_and_current(tmp_path):
    mon, j, *_ = make_monitor(tmp_path)
    assert not run_known(j, "zz")
    register_run(j, "AbC")  # старт демона: запуск известен еще до сделок и решений
    assert run_known(j, "abc")
    add_trade(j, "d1", run_id="r1")  # trades.run_id + decisions.run_id
    assert run_known(j, "R1")
    assert run_known(j, "cur", current_run_id="cur") and not run_known(j, "cur")


def test_own_orphan_is_cancelled_without_safe_mode(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "d1", run_id="r1", status="closed")  # запуск r1 — свой
    register_run(j, "r2")  # запуск r2 упал до первой записи — тоже свой
    ex.open_list = [
        OpenOrderRef("A/USDT:USDT", "o1", "OSKAR-r1-aaaaaaaaaaaa", "entry"),
        OpenOrderRef("B/USDT:USDT", "o2", "OSKAR-r2-bbbbbbbbbbbb-tp", "tp"),
        OpenOrderRef("C/USDT:USDT", "o3", f"OSKAR-{state.run_id}-cccccccccccc-sl", "sl"),  # текущий
    ]
    rep = mon.tick()
    assert sorted(ex.cancelled) == ["o1", "o2", "o3"] and len(rep.orphans) == 3
    assert not state.safe_mode and safe_mode_engaged(j) is None
    assert not any("чужого запуска" in n for n in notes)


def test_foreign_orphan_is_not_cancelled_and_engages_safe_mode(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "d1", run_id="r1", status="closed")
    ex.open_list = [
        OpenOrderRef(SYM, "f1", FOREIGN + "-sl", "sl"),      # стоп позиции второй машины
        OpenOrderRef(SYM, "f2", FOREIGN, "entry"),
        OpenOrderRef("A/USDT:USDT", "o1", "OSKAR-r1-aaaaaaaaaaaa", "entry"),  # свой — снимаем
    ]
    rep = mon.tick()
    assert ex.cancelled == ["o1"] and rep.orphans == ["A/USDT:USDT:entry:o1"]
    assert state.safe_mode
    persisted = safe_mode_engaged(j)  # переживает рестарт (механизм блока H)
    assert persisted is not None and "вторая машина" in persisted["reason"]
    alerts = [n for n in notes if "чужого запуска" in n]
    assert len(alerts) == 1
    assert "ничего не снимаю" in alerts[0] and "новые входы остановлены" in alerts[0]
    assert FOREIGN in alerts[0]

    # следующий скан с тем же набором — без повторного алерта и без снятия
    clock[0] = T0 + timedelta(seconds=ORPHAN_SCAN_SEC + 1)
    ex.cancelled.clear()
    ex.open_list = ex.open_list[:2]  # свой сирота снят
    mon.tick()
    assert ex.cancelled == [] and len([n for n in notes if "чужого запуска" in n]) == 1


def test_foreign_alert_even_if_safe_mode_already_engaged(tmp_path):
    """SAFE MODE по другой причине уже стоит — о второй машине оператор все равно узнает."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    state.safe_mode = True
    ex.open_list = [OpenOrderRef(SYM, "f1", FOREIGN + "-tp", "tp")]
    mon.tick()
    assert ex.cancelled == [] and any("чужого запуска" in n for n in notes)


def test_unreadable_run_lookup_counts_as_foreign(tmp_path, monkeypatch):
    """Журнал не ответил на вопрос «чей запуск» — не снимаем (надежнее, чем снять чужой стоп)."""
    from oskar.core import position_monitor as pm

    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(pm, "run_known", boom)
    ex.open_list = [OpenOrderRef(SYM, "x1", "OSKAR-r1-aaaaaaaaaaaa", "entry")]
    mon.tick()
    assert ex.cancelled == [] and state.safe_mode


# ------------------------------------------------------------------ старт демона


def test_startup_check_runs_before_first_tick(tmp_path):
    """Тот же скан при старте (до runner'а): чужой — SAFE MODE, свой — снят; первый полный
    тик монитора скан не повторяет (интервал отсчитан от старта)."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    register_run(j, "r1")
    ex.open_list = [
        OpenOrderRef(SYM, "f1", FOREIGN + "-sl", "sl"),
        OpenOrderRef("A/USDT:USDT", "o1", "OSKAR-r1-aaaaaaaaaaaa", "entry"),
    ]
    rep = mon.startup_check()
    assert ex.cancelled == ["o1"] and state.safe_mode and rep.orphans
    ex.cancelled.clear()
    mon.tick()
    assert ex.cancelled == []
    assert len([n for n in notes if "чужого запуска" in n]) == 1


def test_startup_check_survives_exchange_failure(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)

    def down():
        raise ConnectionError("exchange unreachable")

    ex.open_orders_all = down
    rep = mon.startup_check()
    assert rep.errors and not state.safe_mode
    calls = []
    ex.open_orders_all = lambda: calls.append(1) or []
    mon.tick()  # первый тик повторяет скан сразу, не через 10 мин
    assert calls == [1]


def test_startup_untracked_position_engages_safe_mode_once(tmp_path):
    """Позиция без сделки в журнале при старте (вторая машина, ручная): не закрываем, SAFE
    MODE + один алерт; reconcile полного тика тот же алерт не повторяет."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    ex.pos = [PositionInfo("ADA/USDT:USDT", "long", 50.0, 0.4, 0.0)]
    rep = mon.startup_check()
    assert state.safe_mode and rep.mismatches and ex.closed == []
    first = list(notes)
    assert any("ADA/USDT:USDT long qty 50" in n for n in first)
    for _ in range(3):
        clock[0] += timedelta(seconds=61)
        mon.tick()
    assert notes == first and ex.closed == []  # ни дубля алерта, ни закрытия


def test_startup_position_of_pending_trade_is_not_untracked(tmp_path):
    """Лимитка исполнилась, пока демон лежал: позиция есть, сделка еще pending — не чужая."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "p1", status="pending")
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 0.0)]
    rep = mon.startup_check()
    assert not state.safe_mode and rep.mismatches == []


def test_startup_position_flicker_is_not_a_mismatch(tmp_path):
    """Один кривой ответ биржи — не расхождение: подтверждение повторным чтением."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    answers = [[PositionInfo("ADA/USDT:USDT", "long", 50.0, 0.4, 0.0)], []]
    ex.positions = lambda: answers.pop(0) if answers else []
    mon.startup_check()
    assert not state.safe_mode


def test_live_trade_orders_are_never_foreign(tmp_path):
    """Ордера живой сделки журнала — свои при любом run_id (сделка — главнее реестра)."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "d1", run_id="old")
    ex.orders["d1-sl"] = OrderState("d1-sl", "open")
    ex.orders["d1-tp"] = OrderState("d1-tp", "open")
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 1.0)]
    ex.open_list = [OpenOrderRef(SYM, "d1-sl", "OSKAR-old-d1-sl", "sl")]
    mon.tick()
    assert ex.cancelled == [] and not state.safe_mode
