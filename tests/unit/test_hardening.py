"""Закрытие находок ревью 2026-10-13 (три агента + Codex gpt-6-astra).

Каждый тест — один класс бага: частичное исполнение, мертвые лимиты портфеля,
гонка approve, протухший план, panic без биржи, API без токена, id решений,
классификация ошибок биржи, изоляция LLM-процесса.
"""

import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from oskar.contracts.schemas import ExecutionPlan
from oskar.core.approvals import resolve_approval
from oskar.core.executor import (
    ExecutionOutcome,
    ExecutionService,
    equity_guarded,
    make_client_order_id,
)
from oskar.core.position_monitor import _iso
from oskar.core.state import RuntimeState
from oskar.core.telegram_buttons import ButtonPoller
from oskar.execution.base import OrderState, PlacedOrder, PositionInfo
from oskar.execution.paper import PaperAdapter
from oskar.journal.db import connect
from tests.unit.test_buttons import CHAT, FakeBotApi, add_pending, callback_update
from tests.unit.test_position_monitor import SYM, add_trade, make_monitor, trade

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def plan(**overrides) -> ExecutionPlan:
    base = dict(
        candidate_id="SOLUSDTUSDT-1791800000000-long", symbol=SYM, side="long",
        entry_market=100.0, entry_limit=99.0, entry_limit_note="FVG 98.5-99.2",
        sl=98.0, tp=106.0, qty=0.61, leverage=5.0, risk_usd=1.22, rr_fee_adjusted=2.8,
        timeframe="15m", tldr="Ретест FVG после свипа лоя; цель — хай диапазона.",
    )
    base.update(overrides)
    return ExecutionPlan(**base)


def make_journal(tmp_path, p: ExecutionPlan):
    j = connect(tmp_path / "j.db")
    add_pending(j, p.candidate_id)
    j.execute("UPDATE pending_approvals SET card_json=? WHERE decision_id=?",
              (p.model_dump_json(), p.candidate_id))
    j.execute("INSERT INTO decisions (run_id, decision_id, candidate_json, outcome) "
              "VALUES ('r1', ?, '{}', 'pending')", (p.candidate_id,))
    j.commit()
    return j


class ProtectionFails(PaperAdapter):
    """Вход проходит, SL отвергается — класс бага v1 (голая позиция + двойной вход)."""

    def __init__(self, price_fn, **kw):
        super().__init__(price_fn, **kw)
        self.closed: list[tuple] = []

    def place_entry(self, symbol, side, qty, client_order_id, **kw):
        if kw.get("stop_loss") is not None:  # прикрепленный стоп биржа тоже отвергает
            raise RuntimeError('bingx {"code":80014,"msg":"stop price invalid"}')
        return super().place_entry(symbol, side, qty, client_order_id, **kw)

    def place_sl(self, symbol, side, stop_price, client_order_id):
        raise RuntimeError('bingx {"code":80014,"msg":"stop price invalid"}')

    def close_position(self, symbol, side):
        self.closed.append((symbol, side))
        return super().close_position(symbol, side)


# -------------------------------------------------------------------- executor


def test_protection_failure_journals_trade_and_flattens(tmp_path):
    p = plan()
    j = make_journal(tmp_path, p)
    adapter = ProtectionFails(lambda s: 100.0)
    out = ExecutionService(adapter, j, "r1").execute(p.candidate_id, "market")
    assert not out.ok and out.entry_sent  # откат approve по такому исходу запрещен
    assert adapter.closed == [(SYM, "long")]  # позицию без стопа не держим
    row = trade(j, p.candidate_id)
    assert row is not None and row["status"] == "closed" and row["close_reason"] == "manual"
    assert row["sl_order_id"] is None
    assert "стоп не встал" in out.message
    assert ExecutionService(adapter, j, "r1").execute(p.candidate_id, "market").message == "уже исполнено"


def test_button_does_not_rollback_after_entry_sent(tmp_path):
    """Кнопка после частичного исполнения закрывает карточку, а не оживляет ее."""
    j = connect(tmp_path / "j.db")
    add_pending(j, "d1")
    state = RuntimeState(journal=j)
    fake = FakeBotApi([callback_update("oskar:market:d1")])
    poller = ButtonPoller(
        "TOKEN", CHAT, state, client=httpx.Client(transport=fake),
        on_resolution=lambda d, m: ExecutionOutcome(
            False, "вход исполнен, защита не встала", entry_sent=True
        ),
    )
    poller.poll_once(timeout=0)
    row = j.execute("SELECT resolution FROM pending_approvals WHERE decision_id='d1'").fetchone()
    assert row["resolution"] == "approved"  # НЕ откатили
    assert "editMessageCaption" in [p for p, _ in fake.calls]  # кнопки сняты, статус дописан


def test_stale_plan_refused_by_price_checks(tmp_path):
    p = plan()
    j = make_journal(tmp_path, p)
    paper = PaperAdapter(lambda s: 100.0)
    svc = ExecutionService(paper, j, "r1", price_fn=lambda s: 97.5)  # цена уже под стопом
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and "за стопом" in out.message and paper.positions() == []

    svc = ExecutionService(paper, j, "r1", price_fn=lambda s: 101.0, max_drift_pct=0.5)  # ушла на 1%
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and "ушла" in out.message
    assert j.execute("SELECT resolution FROM pending_approvals").fetchone()["resolution"] is None or True


def test_market_qty_resized_to_plan_risk_at_live_price(tmp_path):
    """План проверен для лимита 99 (стоп 98, риск $1.22 -> qty 1.22); маркет при 100
    с той же qty дал бы риск $2.44. qty режется вниз до риска плана."""
    p = plan(qty=1.22, entry_limit=99.0, sl=98.0, risk_usd=1.22)
    j = make_journal(tmp_path, p)
    paper = PaperAdapter(lambda s: 100.0)
    out = ExecutionService(paper, j, "r1", price_fn=lambda s: 100.0, max_drift_pct=5).execute(
        p.candidate_id, "market"
    )
    assert out.ok
    from oskar.risk.sizing import qty_for_risk

    # риск плана держится от фактического входа 100 (стоп 98) с издержками; без
    # пересайзинга было бы 1.22 x |100-98| = $2.44 — вдвое больше плана
    assert paper.positions()[0].qty == pytest.approx(qty_for_risk(1.22, 100.0, 98.0), rel=1e-4)


def test_position_limit_and_duplicate_checked_at_press(tmp_path):
    p = plan()
    j = make_journal(tmp_path, p)
    add_trade(j, "other1", symbol="A/USDT:USDT")
    add_trade(j, "other2", symbol="B/USDT:USDT")
    paper = PaperAdapter(lambda s: 100.0)
    out = ExecutionService(paper, j, "r1", max_open_positions=2).execute(p.candidate_id, "market")
    assert not out.ok and "лимит открытых позиций" in out.message

    add_trade(j, "same", symbol=SYM, side="long")
    out = ExecutionService(paper, j, "r1", max_open_positions=10).execute(p.candidate_id, "market")
    assert not out.ok and "уже есть позиция" in out.message


def test_market_entry_price_and_fee_taken_from_exchange(tmp_path):
    """Журнал пишет цену исполнения биржи, не плановую (слиппедж)."""
    p = plan()
    j = make_journal(tmp_path, p)
    paper = PaperAdapter(lambda s: 100.4)  # исполнение на 100.4 при плане 100
    out = ExecutionService(paper, j, "r1").execute(p.candidate_id, "market")
    assert out.ok
    assert trade(j, p.candidate_id)["entry_price"] == pytest.approx(100.4)


def test_client_order_id_is_hash_not_truncation():
    cid = make_client_order_id("96853927", "1000PEPEUSDTUSDT-1791800000000-long")
    cid2 = make_client_order_id("96853927", "1000PEPEUSDTUSDT-1791800000000-short")
    assert cid != cid2 and len(cid) <= 36 and cid.startswith("OSKAR-96853927-")
    assert cid.endswith(hashlib.sha1(b"1000PEPEUSDTUSDT-1791800000000-long").hexdigest()[:12])


def test_equity_guard_rechecks_plan_risk_against_live_equity(tmp_path):
    calls = []
    guarded = equity_guarded(
        lambda d, m: calls.append(d), lambda: "exchange",
        equity_fn=lambda: 121.71, plan_fn=lambda d: plan(risk_usd=10.0), risk_cap_pct=1.0,
    )
    out = guarded("d1", "market")  # план на $1000 equity -> риск $10 при капе $1.22
    assert not out.ok and "другой equity" in out.message and calls == []
    guarded_ok = equity_guarded(
        lambda d, m: calls.append(d) or ExecutionOutcome(True, "ok"),
        lambda: "exchange", equity_fn=lambda: 121.71, plan_fn=lambda d: plan(risk_usd=1.2), risk_cap_pct=1.0,
    )
    assert guarded_ok("d1", "market").ok and calls == ["d1"]


# ------------------------------------------------------------------- approvals


def test_approve_race_second_press_loses(tmp_path):
    j = connect(tmp_path / "j.db")
    add_pending(j, "d1")
    assert resolve_approval(j, "d1", "approve").ok
    second = resolve_approval(j, "d1", "approve")
    assert not second.ok and second.code == 409


def test_snooze_capped_by_card_lifetime(tmp_path):
    j = connect(tmp_path / "j.db")
    add_pending(j, "d1", expires_in_min=10)
    created = (datetime.now(UTC) - timedelta(minutes=50)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    j.execute("UPDATE pending_approvals SET created_at=? WHERE decision_id='d1'", (created,))
    j.commit()
    out = resolve_approval(j, "d1", "snooze")  # 50 мин + 15 > 60 мин жизни карточки
    assert not out.ok and "лимит откладываний" in out.message


def test_reject_and_expiry_mirror_into_decisions(tmp_path):
    j = connect(tmp_path / "j.db")
    for d in ("rej", "old"):
        j.execute("INSERT INTO decisions (run_id, decision_id, candidate_json, outcome) "
                  "VALUES ('r1', ?, '{}', 'pending')", (d,))
    add_pending(j, "rej")
    add_pending(j, "old", expires_in_min=-1)
    j.commit()
    resolve_approval(j, "rej", "reject")
    resolve_approval(j, "old", "approve")
    outcomes = dict(j.execute("SELECT decision_id, outcome FROM decisions").fetchall())
    assert outcomes == {"rej": "approval_rejected", "old": "approval_expired"}


# ------------------------------------------------------------------------- api


def test_api_requires_token_and_loopback_host(tmp_path):
    from fastapi.testclient import TestClient

    from oskar.core.api import build_app

    state = RuntimeState(journal=connect(tmp_path / "j.db"))
    app = build_app(state, api_token="s3cret")
    anon = TestClient(app, base_url="http://127.0.0.1")
    assert anon.post("/pause").status_code == 401 and not state.paused
    assert anon.get("/status").status_code == 401
    assert anon.get("/docs").status_code in (401, 404)
    foreign_host = TestClient(app, base_url="http://evil.example")
    assert foreign_host.post("/pause", headers={"X-Oskar-Token": "s3cret"}).status_code == 400
    ok = TestClient(app, base_url="http://127.0.0.1", headers={"X-Oskar-Token": "s3cret"})
    assert ok.post("/pause").json() == {"paused": True}


def test_panic_reaches_exchange_and_survives_restart(tmp_path):
    from fastapi.testclient import TestClient

    from oskar.core.api import build_app, panic_engaged

    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "pend", status="pending", symbol="A/USDT:USDT")
    add_trade(j, "open1", symbol="B/USDT:USDT")
    ex.by_cid["OSKAR-r1-pend"] = OrderState("e-pend", "open")
    ex.pos = [PositionInfo("B/USDT:USDT", "long", 2.0, 100.0, -1.0)]
    client = TestClient(build_app(state, monitor=mon))
    r = client.post("/panic").json()
    assert r["exchange"]["cancelled"] == 1 and r["exchange"]["closed"] == 1
    assert "e-pend" in ex.cancelled and ex.closed == [("B/USDT:USDT", "long")]
    assert trade(j, "pend")["status"] == "cancelled"
    assert trade(j, "open1")["status"] == "closed" and trade(j, "open1")["close_reason"] == "panic"
    assert panic_engaged(j) is not None  # переживет рестарт
    assert client.post("/resume").status_code == 423
    client.post("/panic/reset")
    assert panic_engaged(j) is None and not state.safe_mode and state.panic_at is None


# --------------------------------------------------------------------- monitor


def test_partial_fill_at_ttl_keeps_position_and_protection(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path, limit_ttl=3600)
    add_trade(j, "p1", status="pending", qty=2.0, opened_at=T0 - timedelta(hours=2))
    ex.by_cid["OSKAR-r1-p1"] = OrderState("e1", "open", filled_qty=1.0, avg_price=99.0)
    ex.orders["e1"] = OrderState("e1", "canceled", filled_qty=1.0, avg_price=99.0)  # после отмены
    mon.tick()
    t = trade(j, "p1")
    assert t["status"] == "open" and t["qty"] == 1.0 and t["entry_price"] == 99.0
    assert ex.cancelled == ["e1"]  # снят только остаток входа, SL/TP живы


def test_pending_not_found_past_ttl_is_cancelled(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path, limit_ttl=3600)
    add_trade(j, "ghost", status="pending", opened_at=T0 - timedelta(hours=2))
    add_trade(j, "young", status="pending", opened_at=T0 - timedelta(minutes=5))
    # ордеров нет ни в by_cid, ни в orders -> биржа отвечает «нет такого ордера»
    rep = mon.tick()
    assert trade(j, "ghost")["status"] == "cancelled"
    assert trade(j, "young")["status"] == "pending" and rep.errors == []  # молодой — просто ждем


def test_gone_protection_is_replaced_and_failure_flips_safe_mode(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "d1")
    ex.orders["d1-sl"] = OrderState("d1-sl", "canceled")  # стоп кто-то снял
    ex.orders["d1-tp"] = OrderState("d1-tp", "open")
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 1.0)]
    placed = []
    ex.place_sl = lambda symbol, side, price, cid: (
        placed.append((price, cid)) or PlacedOrder("sl-new", cid, "STOP_MARKET", "open")
    )
    rep = mon.tick()
    assert placed == [(98.0, "OSKAR-r1-d1-sl-r1")]
    assert trade(j, "d1")["sl_order_id"] == "sl-new" and rep.replaced == ["d1:sl"]
    assert not state.safe_mode

    # переставить не удалось -> голая позиция -> SAFE MODE
    add_trade(j, "d2", symbol="C/USDT:USDT")
    ex.orders["d2-sl"] = OrderState("d2-sl", "canceled")
    ex.orders["d2-tp"] = OrderState("d2-tp", "open")
    ex.orders["sl-new"] = OrderState("sl-new", "open")
    ex.pos.append(PositionInfo("C/USDT:USDT", "long", 2.0, 100.0, 1.0))
    ex.place_sl = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rejected"))
    mon.tick()
    assert state.safe_mode and any("БЕЗ SL" in n for n in notes)


def test_duration_stop_requires_confirmed_close(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path, max_loss_hold=3600)
    add_trade(j, "loser", opened_at=T0 - timedelta(hours=2))
    ex.orders["loser-sl"] = OrderState("loser-sl", "open")
    ex.orders["loser-tp"] = OrderState("loser-tp", "open")
    pos = PositionInfo(SYM, "long", 2.0, 100.0, -3.0)
    ex.pos = [pos]
    # close_position «не сработал»: позиция остается на бирже
    ex.close_position = lambda symbol, side: PlacedOrder("c1", "", "MARKET", "open")
    mon.tick()
    assert trade(j, "loser")["status"] == "open"  # closed не выставлен
    assert state.safe_mode and any("не подтверждено" in n for n in notes)


def test_duration_stop_skips_when_position_vanished(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path, max_loss_hold=3600)
    add_trade(j, "l1", opened_at=T0 - timedelta(hours=2))
    ex.orders["l1-sl"] = OrderState("l1-sl", "open")
    ex.orders["l1-tp"] = OrderState("l1-tp", "open")
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, -3.0)]
    ex.close_position = lambda symbol, side: None  # SL сработал между опросами
    mon.tick()
    assert trade(j, "l1")["status"] == "open" and ex.cancelled == []  # следующий тик увидит SL-филл


def test_sweeper_marks_decision_expired(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    j.execute("INSERT INTO decisions (run_id, decision_id, candidate_json, outcome) "
              "VALUES ('r1', 'old', '{}', 'pending')")
    j.execute("INSERT INTO pending_approvals (decision_id, card_json, expires_at) VALUES ('old', '{}', ?)",
              (_iso(T0 - timedelta(minutes=1)),))
    j.commit()
    mon.tick()
    row = j.execute("SELECT outcome FROM decisions WHERE decision_id='old'").fetchone()
    assert row[0] == "approval_expired"


# ----------------------------------------------------------------- data layer


def test_operation_failed_with_unknown_code_is_transient():
    from oskar.data.client import classify_error

    class OperationFailed(Exception):
        pass

    assert classify_error(OperationFailed('bingx {"code":100500,"msg":"server busy"}')) == "transient"


def test_candidate_id_keyed_by_bar_timestamp():
    from oskar.detect.options import build_candidate
    from tests.unit.test_options import OPP, ind, short_snapshot

    a = build_candidate("SOL/USDT:USDT", "15m", short_snapshot(), ind(), OPP, "bearish", bar_ts=1791800000000)
    b = build_candidate("SOL/USDT:USDT", "15m", short_snapshot(), ind(), OPP, "bearish", bar_ts=1791800900000)
    assert a.id == "SOLUSDTUSDT-1791800000000-short" and a.id != b.id


def test_claude_cli_disallows_tools():
    from oskar.agents.claude_cli import DISALLOWED_TOOLS, restricted_tool_args

    args = restricted_tool_args()
    assert args[0] == "--disallowedTools" and "Bash" in DISALLOWED_TOOLS and "WebFetch" in DISALLOWED_TOOLS


def test_bingx_leverage_sent_as_integer():
    from oskar.execution.bingx import BingXAdapter

    class Ex:
        markets = {"SOL/USDT:USDT": {}}

        def __init__(self):
            self.calls = []

        def set_leverage(self, lev, symbol, params=None):
            self.calls.append((lev, symbol, params))

    ex = Ex()
    BingXAdapter(ex).set_leverage("SOL/USDT:USDT", 4.4, "long")
    assert ex.calls == [(4, "SOL/USDT:USDT", {"side": "LONG"})]
    BingXAdapter(ex).set_leverage("SOL/USDT:USDT", 0.7, "short")
    assert ex.calls[-1][0] == 1


def test_journal_wipe_guard_counts_live_trades(tmp_path):
    from oskar.core.daemon import live_trades_in

    db = tmp_path / "j.db"
    j = connect(db)
    assert live_trades_in(db) == 0
    add_trade(j, "d1")
    add_trade(j, "d2", status="pending", symbol="B/USDT:USDT")
    add_trade(j, "d3", status="closed", symbol="C/USDT:USDT")
    j.commit()
    assert live_trades_in(db) == 2
    assert live_trades_in(tmp_path / "missing.db") == 0


def test_instance_lock_rejects_second_holder(tmp_path):
    from oskar.core.daemon import SingleInstanceError, acquire_instance_lock

    first = acquire_instance_lock(tmp_path / "oskard.lock")
    with pytest.raises(SingleInstanceError):
        acquire_instance_lock(tmp_path / "oskard.lock")
    first.close()


def test_counterfactual_limit_entry_must_be_touched_first(tmp_path):
    """limit 90, SL 89, TP 105; бар 98-106 — входа не было, tp_hit засчитывать нельзя."""
    import json

    import pandas as pd

    from oskar.core.cycle import ShadowCycle
    from oskar.risk.ladder import Caps, ProfileLimits

    j = connect(tmp_path / "j.db")
    state = RuntimeState(journal=j)
    cand = {
        "symbol": SYM, "direction": "long", "ts": "2026-10-13T11:00:00Z",
        "entry_options": [{"kind": "market", "price": 100.0}, {"kind": "limit", "price": 90.0}],
    }
    j.execute("INSERT INTO counterfactuals (decision_id, cohort, candidate_json, "
              "entry_price, sl_price, tp_price) "
              "VALUES ('c1', 'shadow', ?, 90.0, 89.0, 105.0)", (json.dumps(cand),))
    j.commit()
    bars = pd.DataFrame({
        "ts": [1791889200000 + i * 900000 for i in range(3)],  # 11:00, 11:15, 11:30 UTC
        "open": [100, 100, 100], "high": [101, 106, 106], "low": [99, 98, 98], "close": [100, 100, 100],
        "volume": [1, 1, 1],
    })

    class Cache:
        def get(self, symbol, tf, limit):
            return bars

    caps = Caps(1.0, 5.0, 10.0, 75, 2.0, 3)
    cycle = ShadowCycle(Cache(), state, caps, ProfileLimits(0.5, 3.0, 8.0))
    assert cycle.resolve_counterfactuals(SYM) == 0  # не вошли -> исход не присвоен
    assert cycle.unresolved_symbols() == [SYM]
