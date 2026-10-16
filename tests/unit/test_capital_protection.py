"""Блок H (2026-10-16): защита капитала в автомате.

H1 — R:R с комиссиями и дистанция стопа перепроверяются по ФАКТИЧЕСКОЙ цене входа той же
функцией, что у лестницы; H2 — стоп по просадке (пик equity, персистентная защелка,
ручное снятие); H4 — SAFE MODE переживает рестарт; H5 — алерты доходят при «&»/«<» в
тексте; H6 — panic/SAFE MODE/просадка перед самой отправкой входа; H7 — белый список
среды (только paper/VST); H8 — предохранитель автомата по машинному коду исхода.
"""

import json
import logging
import sys

import httpx
import pytest

from oskar import cli
from oskar.contracts.schemas import ExecutionPlan
from oskar.core.emergency import (
    engage_panic,
    engage_safe_mode,
    reset_panic,
    restore_safe_mode,
    safe_mode_engaged,
)
from oskar.core.executor import (
    KIND_ENTRY_UNKNOWN,
    KIND_EXCHANGE_REJECTED,
    KIND_OK,
    KIND_PROTECTION_FAILED,
    KIND_REFUSED,
    ExecutionOutcome,
    ExecutionService,
)
from oskar.core.state import RuntimeState
from oskar.core.telegram_buttons import status_line
from oskar.core.telegram_out import TelegramSender
from oskar.core.trading_mode import AUTO, BREAKER_FAILURES, CONFIRM, AutoExecutor, TradingMode
from oskar.execution.base import OrderState, PositionInfo
from oskar.journal.db import connect
from oskar.risk.drawdown import (
    HALT_KEY,
    DrawdownError,
    DrawdownGuard,
    read_halt,
    read_peak,
)
from oskar.risk.ladder import check_entry_price
from oskar.risk.sizing import fee_adjusted_rr
from tests.unit.test_buttons import add_pending
from tests.unit.test_emergency import LockedJournal
from tests.unit.test_escalation import CAPS, PROFILE
from tests.unit.test_position_monitor import SYM, add_trade, make_monitor, trade
from tests.unit.test_protection_lifecycle import ExchangeError, ScriptedExchange
from tests.unit.test_protection_lifecycle import make_service as make_bera_service


def h_plan(**overrides) -> ExecutionPlan:
    """Пример из задачи: long, вход 100, SL 99, TP 103.2 — лестница видела R:R 3."""
    base = dict(
        candidate_id="SOLUSDTUSDT-1-long", symbol="SOL/USDT:USDT", side="long",
        entry_market=100.0, entry_limit=None, sl=99.0, tp=103.2,
        qty=3.0, leverage=3.0, risk_usd=3.0, rr_fee_adjusted=3.0, timeframe="15m",
    )
    base.update(overrides)
    return ExecutionPlan(**base)


def service(tmp_path, p, *, price=None, caps=True, ex=None, db="j.db", max_drift_pct=0.5):
    """Исполнитель с капсами/профилем (как в демоне) над ScriptedExchange; price — список [px]."""
    j = connect(tmp_path / db)
    add_pending(j, p.candidate_id)
    j.execute("UPDATE pending_approvals SET card_json=? WHERE decision_id=?",
              (p.model_dump_json(), p.candidate_id))
    j.execute("INSERT INTO decisions (run_id, decision_id, candidate_json, outcome) "
              "VALUES ('r1', ?, '{}', 'pending')", (p.candidate_id,))
    j.commit()
    ex = ex or ScriptedExchange()
    state = RuntimeState(journal=j)
    svc = ExecutionService(
        ex, j, "r1",
        price_fn=(lambda s: price[0]) if price is not None else None,
        max_drift_pct=max_drift_pct,
        caps=CAPS if caps else None, profile=PROFILE if caps else None,
        state=state, sleep=lambda s: None,
    )
    return svc, j, ex, state


def entries(ex) -> list:
    return [c for c in ex.calls if c[0] == "entry"]


def trades_count(j) -> int:
    return j.execute("SELECT COUNT(*) FROM trades").fetchone()[0]


# ========================================================= H1: R:R по фактической цене


def test_market_rr_rechecked_at_live_price_long(tmp_path):
    """Пример из задачи: дрейф 0.49% проверку дрейфа проходит, но R:R по 100.49 — 1.68 < 2.0."""
    p = h_plan()
    assert fee_adjusted_rr(100.0, 99.0, 103.2) == pytest.approx(3.0)
    px = [100.49]
    svc, j, ex, _ = service(tmp_path, p, price=px)
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and out.kind == KIND_REFUSED and not out.entry_sent
    assert "R:R по текущей цене 1.68 < 2.0 — вход отменен" in out.message
    assert entries(ex) == [] and trades_count(j) == 0
    px[0] = 100.0  # цена вернулась к плану — вход разрешен
    assert svc.execute(p.candidate_id, "market").ok


def test_market_rr_rechecked_at_live_price_short(tmp_path):
    p = h_plan(candidate_id="SOLUSDTUSDT-1-short", side="short", sl=101.0, tp=96.8)
    svc, j, ex, _ = service(tmp_path, p, price=[99.51])
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and "R:R по текущей цене 1.69 < 2.0 — вход отменен" in out.message
    assert entries(ex) == []


def test_limit_plan_market_button_checked_at_market_price(tmp_path):
    """Trader выбрал лимитку 100 (лестница — по ней), но у карточки есть и «Маркет»:
    по рынку 100.4 R:R 1.29 — маркет отказ, лимит по уровню лестницы проходит."""
    p = h_plan(entry_limit=100.0, entry_market=100.4, tp=102.4)
    assert check_entry_price("long", 100.0, 99.0, 102.4, CAPS, PROFILE).failed is None
    svc, j, ex, _ = service(tmp_path, p, price=[100.4])
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and "R:R по текущей цене 1.29 < 2.0 — вход отменен" in out.message
    assert entries(ex) == []
    out = svc.execute(p.candidate_id, "limit")
    assert out.ok, out.message
    assert entries(ex)[0][1] == "limit"


def test_limit_level_from_menu_too_close_to_stop_refused(tmp_path):
    """Trader выбрал маркет (лестница — по 100), лимит-уровень FVG из меню 100.6 у самого стопа
    101: стоп 0.40% < минимума профиля — лимит отказ, маркет проходит."""
    p = h_plan(candidate_id="SOLUSDTUSDT-2-short", side="short", entry_market=100.0,
               entry_limit=100.6, sl=101.0, tp=97.5)
    svc, j, ex, _ = service(tmp_path, p, price=[100.0])
    out = svc.execute(p.candidate_id, "limit")
    assert not out.ok and "стоп по лимитному уровню 0.40% вне [0.5..3.0]% — вход отменен" in out.message
    assert entries(ex) == []
    assert svc.execute(p.candidate_id, "market").ok


def test_limit_level_with_worse_rr_refused(tmp_path):
    p = h_plan(entry_market=100.0, entry_limit=100.2, tp=102.3)
    svc, j, ex, _ = service(tmp_path, p, price=[100.0])
    out = svc.execute(p.candidate_id, "limit")
    assert not out.ok and "R:R по лимитному уровню 1.58 < 2.0 — вход отменен" in out.message


@pytest.mark.parametrize(("px", "passes"), [
    (100.33, True),     # R:R 2.007
    (100.3353, True),   # R:R 1.995: ниже 2.0, но в допуске RR_TOLERANCE — как у лестницы
    (100.34, False),    # R:R 1.985: за допуском
])
def test_rr_boundary_matches_ladder(tmp_path, px, passes):
    """Граница: исполнитель решает ровно как шаг лестницы (одна функция, тот же допуск)."""
    ladder = check_entry_price("long", px, 99.0, 103.2, CAPS, PROFILE)
    assert (ladder.failed is None) is passes
    p = h_plan(entry_market=px)
    svc, j, ex, _ = service(tmp_path, p, price=[px])
    out = svc.execute(p.candidate_id, "market")
    assert out.ok is passes, out.message
    if not passes:
        assert "R:R по текущей цене 1.98 < 2.0" in out.message


def test_sl_distance_upper_bound_at_live_price(tmp_path):
    """Цена ушла от стопа: дистанция 3.2% > максимума профиля 3.0% (дрейф сам по себе разрешен)."""
    p = h_plan(sl=97.0, tp=112.0)
    svc, j, ex, _ = service(tmp_path, p, price=[100.2], max_drift_pct=1.0)
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and "стоп по текущей цене 3.19% вне [0.5..3.0]% — вход отменен" in out.message


def test_check_disabled_without_caps_and_needs_both(tmp_path):
    p = h_plan()
    svc, j, ex, _ = service(tmp_path, p, price=[100.49], caps=False)
    assert svc.execute(p.candidate_id, "market").ok  # обратная совместимость: None = выключено
    with pytest.raises(ValueError, match="вместе"):
        ExecutionService(ex, j, "r1", caps=CAPS)


# ================================================================ H2: стоп по просадке


class Equity:
    def __init__(self, value=1000.0, source="exchange"):
        self.value, self.source = value, source


def guard(j, eq: Equity, pct=10) -> DrawdownGuard:
    return DrawdownGuard(j, pct, equity_fn=lambda: eq.value, equity_source_fn=lambda: eq.source)


def test_loss_series_latches_once_and_never_lifts_itself(tmp_path):
    j = connect(tmp_path / "j.db")
    eq = Equity(1000.0)
    g = guard(j, eq)
    fired = []
    for value in (1000.0, 1050.0, 1000.0, 960.0, 945.0, 900.0, 1100.0):
        eq.value = value
        fired.append(g.check())
    halts = [h for h in fired if h is not None]
    assert len(halts) == 1  # алерт один раз — на 945 = 1050 * 0.9
    assert halts[0].equity == 945.0 and halts[0].peak == 1050.0
    assert "equity 945.00 от пика 1050.00 (−10.0%)" in halts[0].message()
    assert read_halt(j) is not None  # 1100 > пика — защелка все равно стоит (урок v1)
    assert read_peak(j) == 1050.0  # пик под защелкой не двигается


def test_latch_survives_restart(tmp_path):
    j = connect(tmp_path / "j.db")
    eq = Equity(1000.0)
    g = guard(j, eq)
    g.check()
    eq.value = 850.0
    assert g.check() is not None
    j.close()
    j2 = connect(tmp_path / "j.db")  # рестарт: новый журнал-коннект и новый объект
    g2 = guard(j2, Equity(1200.0))
    assert g2.check() is None and read_halt(j2).peak == 1000.0


def test_no_exchange_equity_no_peak_no_latch(tmp_path):
    """Дефолтные $1000 до ответа биржи при пике $100k — не просадка."""
    j = connect(tmp_path / "j.db")
    g = guard(j, Equity(100000.0))
    g.check()
    g2 = guard(j, Equity(1000.0, source="default"))
    assert g2.check() is None and read_halt(j) is None and read_peak(j) == 100000.0
    with pytest.raises(DrawdownError, match="default"):
        g2.reset()


@pytest.mark.parametrize("pct", [0, 100, -5, "x"])
def test_guard_rejects_bad_pct(tmp_path, pct):
    with pytest.raises(ValueError):
        guard(connect(tmp_path / "j.db"), Equity(), pct=pct)


def _latched(j, equity=945.0, peak=1050.0):
    j.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
              (HALT_KEY, json.dumps({"equity": equity, "peak": peak, "at": "2026-10-16T10:00:00Z"})))
    j.commit()


def test_executor_refuses_new_entry_while_latched_and_reset_lifts(tmp_path):
    p = h_plan()
    svc, j, ex, _ = service(tmp_path, p, price=[100.0])
    g = guard(j, Equity(1050.0))
    g.check()
    _latched(j)
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and out.kind == KIND_REFUSED and entries(ex) == []
    assert out.message == ("стоп по просадке: equity 945.00 от пика 1050.00 (−10.0%) — "
                           "новые входы заблокированы до ручного снятия")
    g2 = guard(j, Equity(945.0))
    res = g2.reset()
    assert res == {"drawdown_halt": False, "was_halted": True, "equity": 945.0,
                   "peak_before": 1050.0, "peak": 945.0}
    assert read_halt(j) is None and read_peak(j) == 945.0
    assert svc.execute(p.candidate_id, "market").ok


def test_monitor_keeps_managing_positions_while_latched(tmp_path):
    """Защелка закрывает входы, но монитор закрывает сделку по стопу как обычно; алерт — один."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    eq = Equity(1000.0)
    mon._drawdown = guard(j, eq)
    add_trade(j, "d1", entry=100.0, qty=2.0, sl=98.0)
    ex.orders["d1-sl"] = OrderState("d1-sl", "open")
    ex.orders["d1-tp"] = OrderState("d1-tp", "open")
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, -1.0)]
    mon.tick()
    eq.value = 880.0
    mon.tick()
    assert read_halt(j) is not None and sum("стоп по просадке" in n for n in notes) == 1
    ex.orders["d1-sl"] = OrderState("d1-sl", "filled", 2.0, 98.0, fee=0.1)
    ex.pos = []
    rep = mon.tick()
    assert rep.closed == [("d1", "sl_hit")] and trade(j, "d1")["status"] == "closed"
    assert sum("стоп по просадке" in n for n in notes) == 1  # не повторяется каждый тик


def test_status_api_and_telegram_show_latch_and_api_reset(tmp_path):
    from fastapi.testclient import TestClient

    from oskar.core.api import build_app

    j = connect(tmp_path / "j.db")
    state = RuntimeState(journal=j, run_id="r1")
    eq = Equity(1050.0)
    g = guard(j, eq)
    g.check()
    _latched(j)
    client = TestClient(build_app(state, drawdown=g))
    payload = client.get("/status").json()
    assert payload["drawdown"]["peak"] == 1050.0 and payload["drawdown"]["halt"]["pct"] == 10.0
    assert "СТОП ПО ПРОСАДКЕ −10.0% от пика $1050.00" in status_line(state)
    eq.source = "persisted"
    r = client.post("/drawdown/reset")
    assert r.status_code == 409 and read_halt(j) is not None  # без equity с биржи ничего не тронуто
    eq.source, eq.value = "exchange", 950.0
    r = client.post("/drawdown/reset")
    assert r.status_code == 200 and r.json()["peak"] == 950.0 and read_halt(j) is None
    assert client.get("/status").json()["drawdown"]["halt"] is None
    assert TestClient(build_app(state)).post("/drawdown/reset").status_code == 409


def test_cli_drawdown_reset_prints_equity_and_peak(monkeypatch, capsys):
    calls = []

    def fake_call(method, path, timeout):
        calls.append((method, path))
        return 200, {"drawdown_halt": False, "was_halted": True, "equity": 945.0,
                     "peak_before": 1050.0, "peak": 945.0}

    monkeypatch.setattr(cli, "_daemon_api_call", fake_call)
    monkeypatch.setattr(sys, "argv", ["oskar", "drawdown-reset"])
    code = cli.main()
    out = capsys.readouterr().out
    assert code == 0 and calls == [("POST", "/drawdown/reset")] and out.isascii()
    assert "was ENGAGED - lifted: equity=945.00 peak 1050.00 -> 945.00" in out


# ============================================================ H4: SAFE MODE после рестарта


def test_monitor_safe_mode_survives_restart_until_explicit_reset(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    mon._set_safe_mode("позиция SOL/USDT:USDT без защиты")
    assert state.safe_mode and safe_mode_engaged(j)["reason"] == "позиция SOL/USDT:USDT без защиты"
    restarted = RuntimeState(journal=j)  # рестарт демона (супервизор блока G)
    assert not restarted.safe_mode
    alerts = []
    assert restore_safe_mode(restarted, notify=alerts.append) == "позиция SOL/USDT:USDT без защиты"
    assert restarted.safe_mode
    assert alerts == ["🛑 SAFE MODE восстановлен после рестарта: позиция SOL/USDT:USDT без защиты"]
    reset_panic(restarted)  # снятие — только явное (oskar panic-reset)
    again = RuntimeState(journal=j)
    assert restore_safe_mode(again) is None and not again.safe_mode


def test_runner_safe_mode_persisted_and_alert_escaped(tmp_path):
    from tests.unit.test_equity_guard import make_runner

    alerts = []
    runner, state, _ = make_runner(tmp_path, equity_fn=None)
    runner._notify = alerts.append
    runner._enter_safe_mode("3 consecutive shadow failures (cycle: a<b & c)")
    assert state.safe_mode and "a<b & c" in safe_mode_engaged(state.journal)["reason"]
    assert alerts and "a&lt;b &amp; c" in alerts[0]
    restarted = RuntimeState(journal=state.journal)
    restore_safe_mode(restarted)
    assert restarted.safe_mode


def test_engage_safe_mode_survives_journal_failure():
    state = RuntimeState(journal=LockedJournal())
    assert engage_safe_mode(state, "x") and state.safe_mode  # флаг в памяти стоит
    assert not engage_safe_mode(state, "y")  # второй раз — не взвод (алерт один)


# ===================================================================== H5: алерты


class TelegramStub(httpx.BaseTransport):
    """HTML-режим отвергает сырые «&»/«<» как настоящий Telegram; status — для HTTP-ошибок."""

    def __init__(self, status=200):
        self.bodies: list[dict] = []
        self.status = status

    def handle_request(self, request):
        request.read()
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.status != 200 and len(self.bodies) == 1:
            return httpx.Response(self.status, text="<html>bad gateway</html>")
        text = body["text"]
        if body.get("parse_mode") == "HTML" and ("<" in text or "&" in text.replace("&amp;", "")
                                                 .replace("&lt;", "").replace("&gt;", "")):
            return httpx.Response(400, json={"ok": False, "error_code": 400,
                                             "description": "Bad Request: can't parse entities"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})


def test_send_text_falls_back_to_plain_text_when_html_refused(caplog):
    stub = TelegramStub()
    sender = TelegramSender("TOKEN", "1", httpx.Client(transport=stub))
    with caplog.at_level(logging.ERROR, logger="oskar.telegram"):
        sent = sender.send_text("🛑 SOL БЕЗ SL: [80014] stop <price> & qty")
    assert sent.ok and sent.message_id == 7
    assert [b.get("parse_mode") for b in stub.bodies] == ["HTML", None]
    assert stub.bodies[1]["text"] == "🛑 SOL БЕЗ SL: [80014] stop <price> & qty"
    assert any("can't parse entities" in r.getMessage() for r in caplog.records)
    assert all("TOKEN" not in r.getMessage() for r in caplog.records)


def test_send_text_retries_after_http_error():
    stub = TelegramStub(status=502)
    sender = TelegramSender("TOKEN", "1", httpx.Client(transport=stub))
    assert sender.send_text("alert").ok and len(stub.bodies) == 2


def test_monitor_alert_without_sl_is_short_and_escaped(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "d1")
    j.execute("UPDATE trades SET sl_order_id=NULL WHERE decision_id='d1'")
    j.commit()
    ex.orders["d1-tp"] = OrderState("d1-tp", "open")
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 0.5)]
    url = "https://open-api-vst.bingx.com/openApi/swap/v2/trade/order?a=1&signature=" + "f" * 64

    def boom(*a, **k):
        raise ExchangeError('bingx {"code":80014,"msg":"stop <price> invalid & qty"} ' + url)

    ex.place_sl = boom
    mon.tick()
    alert = next(n for n in notes if "БЕЗ SL" in n)
    assert "[80014]" in alert and "&lt;price&gt;" in alert and "&amp; qty" in alert
    assert "<" not in alert and "signature" not in alert  # short_error режет хвост с подписью
    assert state.safe_mode and safe_mode_engaged(j) is not None  # H4: и это переживет рестарт


# ===================================================== H6: флаги перед отправкой входа


def test_panic_between_plan_and_entry_blocks_order(tmp_path):
    """panic нажат, пока исполнитель готовит вход (после предпроверок): ордер не уходит."""
    p = h_plan()
    svc, j, ex, state = service(tmp_path, p, price=[100.0])
    orig = ex.set_leverage

    def leverage_then_panic(*a):
        orig(*a)
        state.panic()  # Telegram /panic в этот момент

    ex.set_leverage = leverage_then_panic
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and out.kind == KIND_REFUSED and "panic взведен — вход отменен" in out.message
    assert ("leverage", 3.0) in ex.calls and entries(ex) == [] and trades_count(j) == 0


def test_panic_from_other_surface_in_journal_blocks_after_plan_read(tmp_path):
    """panic из API/CLI (флаг в meta, другой объект состояния) — между plan_for и входом."""
    p = h_plan()
    svc, j, ex, _ = service(tmp_path, p, price=[100.0])
    orig = svc.plan_for

    def plan_then_panic(decision_id):
        plan = orig(decision_id)
        engage_panic(RuntimeState(journal=j))
        return plan

    svc.plan_for = plan_then_panic
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and "panic взведен" in out.message and entries(ex) == []


def test_safe_mode_blocks_entry(tmp_path):
    p = h_plan()
    svc, j, ex, state = service(tmp_path, p, price=[100.0])
    engage_safe_mode(RuntimeState(journal=j), "журнал и биржа расходятся")  # монитор, другой поток
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and "SAFE MODE взведен — вход отменен" in out.message and entries(ex) == []


# ================================================================ H7: белый список среды


@pytest.mark.parametrize(("mode", "sandbox"), [
    ("live", True), ("Live", True), ("real", False), ("paper", False), ("paper", "true"),
    ("paper", None), ("Paper", True), (None, True), ("paper ", 1),
])
def test_paper_gate_refuses_everything_but_paper_vst(mode, sandbox):
    from oskar.core.env_gate import paper_gate

    reason = paper_gate({"mode": mode, "exchange": {"sandbox": sandbox}})
    assert reason is not None and "гейтом Фазы 5 (60 сделок, PF ≥ 1.3)" in reason


def test_paper_gate_allows_only_paper_with_bool_sandbox():
    from oskar.core.env_gate import paper_gate

    assert paper_gate({"mode": "paper", "exchange": {"sandbox": True}}) is None
    assert paper_gate({"mode": " paper ", "exchange": {"sandbox": True}}) is None  # strip
    assert "режим 'live' не разрешен" in paper_gate({"mode": "live", "exchange": {"sandbox": True}})
    assert paper_gate({}) is not None


def test_daemon_and_factories_use_the_gate():
    from oskar.core import daemon
    from oskar.data.client import make_bingx_exchange
    from oskar.sync.exchange_sync import make_bingx_view

    assert daemon._sandbox({"mode": "paper", "exchange": {"sandbox": True}}) is True
    with pytest.raises(RuntimeError, match="Фазы 5"):
        daemon._sandbox({"mode": "real", "exchange": {"sandbox": False}})
    for sandbox in (False, None, "true"):
        with pytest.raises(RuntimeError, match="Фазы 5"):
            make_bingx_exchange("k", "s", sandbox=sandbox)
        with pytest.raises(RuntimeError, match="Фазы 5"):
            make_bingx_view("k", "s", sandbox=sandbox)


def test_repo_settings_pass_the_gate():
    from oskar.core.bootstrap import load_settings
    from oskar.core.env_gate import paper_gate

    assert paper_gate(load_settings()) is None


# ======================================================== H8: предохранитель автомата


def _auto(tmp_path, outcomes, notify=None):
    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    mode = TradingMode(state.journal, allowed=True, default=AUTO)
    it = iter(outcomes)
    return AutoExecutor(mode, state, lambda d, m: next(it), notify=notify), state, mode


def _run(auto, state, n):
    results = []
    for i in range(n):
        add_pending(state.journal, f"d{i}")
        results.append(auto.run(f"d{i}", "market"))
    return results


def test_protection_failed_trips_breaker_at_once(tmp_path):
    notes = []
    out = ExecutionOutcome(False, "⚠️ вход исполнен, но стоп не встал ([80014] x); позиция закрыта",
                           entry_sent=True, kind=KIND_PROTECTION_FAILED)
    auto, state, mode = _auto(tmp_path, [out], notify=notes.append)
    res = _run(auto, state, 1)
    assert res[0].executed  # вход был — approve не откатываем
    assert mode.get() == CONFIRM and any("защита не встала" in n for n in notes)


def test_precheck_refusals_never_trip_breaker(tmp_path):
    outs = [ExecutionOutcome(False, "R:R по текущей цене 1.68 < 2.0 — вход отменен", kind=KIND_REFUSED)
            for _ in range(BREAKER_FAILURES * 2)]
    auto, state, mode = _auto(tmp_path, outs)
    _run(auto, state, len(outs))
    assert mode.get() == AUTO


REJECTED = ExecutionOutcome(False, "биржа отказала: [80001] bad", kind=KIND_EXCHANGE_REJECTED)
UNKNOWN = ExecutionOutcome(False, "биржа не ответила на вход", entry_sent=True, kind=KIND_ENTRY_UNKNOWN)


def test_success_between_exchange_failures_resets_series(tmp_path):
    ok = ExecutionOutcome(True, "маркет: SOL", entry_sent=True, kind=KIND_OK)
    auto, state, mode = _auto(tmp_path, [REJECTED, UNKNOWN, ok, REJECTED, REJECTED])
    _run(auto, state, 5)
    assert mode.get() == AUTO


def test_exchange_failures_in_a_row_trip_breaker(tmp_path):
    auto, state, mode = _auto(tmp_path, [REJECTED, UNKNOWN, REJECTED])
    results = _run(auto, state, 3)
    assert results[1].executed  # таймаут входа: запись в журнале есть — кнопки не нужны
    assert mode.get() == CONFIRM  # но это сбой биржи и он в серии


def test_executor_outcomes_carry_kind(tmp_path):
    ex = ScriptedExchange()
    ex.entry_error = ExchangeError('bingx {"code":80001,"msg":"bad param"}')
    svc, j, p, _ = make_bera_service(tmp_path, ex)
    assert svc.execute(p.candidate_id, "market").kind == KIND_EXCHANGE_REJECTED

    ex2 = ScriptedExchange()
    ex2.reject_attached = ExchangeError('bingx {"code":80001,"msg":"bad param"}')
    ex2.sl_errors = [ExchangeError('bingx {"code":80014,"msg":"stop price invalid"}')]
    (tmp_path / "b").mkdir()
    svc2, j2, p2, _ = make_bera_service(tmp_path / "b", ex2)
    out = svc2.execute(p2.candidate_id, "market")
    assert out.entry_sent and out.kind == KIND_PROTECTION_FAILED

    assert svc2.execute(p2.candidate_id, "market").kind == KIND_REFUSED  # «уже исполнено»
    assert ExecutionOutcome(False, "что-то").kind == "error"  # без кода — консервативно сбой
    assert ExecutionOutcome(True, "ok").kind == KIND_OK
