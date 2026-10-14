"""Монитор позиций: журнал догоняет биржу по состоянию ордеров (ст. 1)."""

from datetime import UTC, datetime, timedelta

from oskar.core.position_monitor import PositionMonitor, _iso
from oskar.core.state import RuntimeState
from oskar.execution.base import (
    BalanceInfo,
    ExchangeAdapter,
    OpenOrderRef,
    OrderNotFoundError,
    OrderState,
    PlacedOrder,
    PositionInfo,
    ProtectiveOrder,
)
from oskar.execution.paper import PaperAdapter
from oskar.journal.db import connect

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
SYM = "SOL/USDT:USDT"


class FakeAdapter(ExchangeAdapter):
    """Биржа по сценарию: состояния ордеров и позиции задаются тестом."""

    def __init__(self):
        self.orders: dict[str, OrderState] = {}
        self.by_cid: dict[str, OrderState] = {}
        self.pos: list[PositionInfo] = []
        self.cancelled: list[str] = []
        self.closed: list[tuple[str, str]] = []
        self.fail_symbols: set[str] = set()
        self.close_price = 97.0
        self.protection: list[ProtectiveOrder] = []  # SL/TP «на бирже» (в т.ч. прикрепленные)
        self.open_list: list[OpenOrderRef] = []  # все открытые ордера аккаунта

    def normalize_qty(self, symbol, qty):
        return qty

    def set_leverage(self, symbol, leverage, side):
        pass

    def place_entry(self, *a, **k):
        raise NotImplementedError

    def place_sl(self, *a, **k):
        raise NotImplementedError

    def place_tp(self, *a, **k):
        raise NotImplementedError

    def cancel_all(self, symbol):
        return 0

    def close_position(self, symbol, side):
        self.closed.append((symbol, side))
        self.pos = [p for p in self.pos if not (p.symbol == symbol and p.side == side)]
        self.orders["close-1"] = OrderState("close-1", "filled", 2.0, self.close_price, 0.2)
        return PlacedOrder("close-1", "", "MARKET", "closed")

    def positions(self):
        return list(self.pos)

    def balance(self):
        return BalanceInfo(100.0, 100.0, 0.0)

    def order_status(self, symbol, exchange_id="", client_order_id=""):
        if symbol in self.fail_symbols:
            raise RuntimeError("exchange hiccup")
        book, key = (self.orders, exchange_id) if exchange_id else (self.by_cid, client_order_id)
        if key not in book:
            raise OrderNotFoundError(f"no order {key}")  # как у биржи: «нет такого» — не сбой сети
        return book[key]

    def protection_orders(self, symbol, side, *, include_closed=False):
        return [o for o in self.protection if include_closed or o.status == "open"]

    def open_orders_all(self):
        return list(self.open_list)

    def cancel_order(self, symbol, exchange_id):
        self.cancelled.append(exchange_id)
        return True


def add_trade(j, decision_id, *, status="open", symbol=SYM, side="long", entry=100.0,
              qty=2.0, sl=98.0, tp=104.0, opened_at=T0, run_id="r1"):
    j.execute(
        "INSERT INTO trades (run_id, decision_id, client_order_id, symbol, side, status, "
        "entry_price, qty, leverage, risk_usd, sl_price, tp_price, sl_order_id, tp_order_id, "
        "setup_type, timeframe, reasoning, opened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, decision_id, f"OSKAR-{run_id}-{decision_id}", symbol, side, status, entry, qty,
         5.0, 1.0, sl, tp, f"{decision_id}-sl", f"{decision_id}-tp", "", "15m", "tldr", _iso(opened_at)),
    )
    j.execute(
        "INSERT INTO decisions (run_id, decision_id, candidate_json, outcome) "
        "VALUES (?, ?, '{}', 'executed')", (run_id, decision_id),
    )
    j.commit()


def trade(j, decision_id):
    return j.execute("SELECT * FROM trades WHERE decision_id=?", (decision_id,)).fetchone()


def make_monitor(tmp_path, *, now=None, max_loss_hold=3 * 3600, limit_ttl=3600):
    j = connect(tmp_path / "j.db")
    ex = FakeAdapter()
    clock = [now or T0]
    notes: list[str] = []
    expired: list[tuple] = []
    state = RuntimeState(journal=j)
    mon = PositionMonitor(
        ex, j, max_loss_hold_seconds=max_loss_hold, limit_entry_ttl_seconds=limit_ttl,
        notify=notes.append, on_card_expired=lambda d, m: expired.append((d, m)),
        state=state, now_fn=lambda: clock[0],
    )
    return mon, j, ex, clock, notes, expired, state


# --------------------------------------------------------------------- sweeper


def test_sweeper_expires_stale_cards_and_removes_buttons(tmp_path):
    mon, j, ex, clock, notes, expired, state = make_monitor(tmp_path)
    j.execute(
        "INSERT INTO pending_approvals (decision_id, card_json, expires_at, telegram_message_id) "
        "VALUES ('old', '{}', ?, '77')", (_iso(T0 - timedelta(minutes=5)),),
    )
    j.execute(
        "INSERT INTO pending_approvals (decision_id, card_json, expires_at) VALUES ('fresh', '{}', ?)",
        (_iso(T0 + timedelta(minutes=10)),),
    )
    j.commit()
    rep = mon.tick()
    rows = dict(j.execute("SELECT decision_id, resolution FROM pending_approvals").fetchall())
    assert rows == {"old": "expired", "fresh": None}
    assert rep.expired_cards == 1 and expired == [("old", "77")]


# --------------------------------------------------------------------- pending


def test_pending_limit_filled_becomes_open(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "p1", status="pending", entry=99.0)
    ex.by_cid["OSKAR-r1-p1"] = OrderState("e1", "filled", 2.0, 99.5, fee=0.05)
    rep = mon.tick()
    t = trade(j, "p1")
    assert t["status"] == "open" and t["entry_price"] == 99.5 and t["entry_fees"] == 0.05
    assert rep.filled == ["p1"] and any("лимит исполнен" in n for n in notes)


def test_pending_limit_cancelled_after_ttl_with_protection(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path, limit_ttl=3600)
    add_trade(j, "old", status="pending", opened_at=T0 - timedelta(hours=2))
    add_trade(j, "young", status="pending", opened_at=T0 - timedelta(minutes=10))
    ex.by_cid["OSKAR-r1-old"] = OrderState("e-old", "open")
    ex.by_cid["OSKAR-r1-young"] = OrderState("e-young", "open")
    rep = mon.tick()
    assert trade(j, "old")["status"] == "cancelled"
    assert trade(j, "young")["status"] == "pending"  # еще ждет цену
    assert set(ex.cancelled) == {"e-old", "old-sl", "old-tp"}
    detail = j.execute("SELECT outcome_detail FROM decisions WHERE decision_id='old'").fetchone()[0]
    assert detail.startswith("limit_cancelled")
    assert rep.cancelled == ["old"]


def test_pending_limit_removed_by_exchange(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "p1", status="pending")
    ex.by_cid["OSKAR-r1-p1"] = OrderState("e1", "canceled")
    mon.tick()
    assert trade(j, "p1")["status"] == "cancelled"
    assert "e1" not in ex.cancelled and set(ex.cancelled) == {"p1-sl", "p1-tp"}


# ------------------------------------------------------------------------ open


def test_sl_hit_closes_long_with_pnl_and_cancels_tp(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "d1", entry=100.0, qty=2.0, sl=98.0)
    ex.orders["d1-sl"] = OrderState("d1-sl", "filled", 2.0, 98.0, fee=0.1)
    ex.orders["d1-tp"] = OrderState("d1-tp", "open")
    clock[0] = T0 + timedelta(minutes=45)
    rep = mon.tick()
    t = trade(j, "d1")
    assert t["status"] == "closed" and t["close_reason"] == "sl_hit"
    assert t["exit_price"] == 98.0 and abs(t["realized_pnl"] - (-4.0 - 0.1)) < 1e-9
    assert t["duration_seconds"] == 45 * 60 and t["reconciled"] == 0  # «сверено» — только по филлам
    assert ex.cancelled == ["d1-tp"]
    assert rep.closed == [("d1", "sl_hit")] and any("стоп" in n for n in notes)


def test_tp_hit_closes_short_with_positive_pnl(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "s1", side="short", entry=100.0, qty=2.0, sl=103.0, tp=95.0)
    ex.orders["s1-sl"] = OrderState("s1-sl", "open")
    ex.orders["s1-tp"] = OrderState("s1-tp", "filled", 2.0, 95.0)
    mon.tick()
    t = trade(j, "s1")
    assert t["close_reason"] == "tp_hit" and abs(t["realized_pnl"] - 10.0) < 1e-9
    assert ex.cancelled == ["s1-sl"]


def test_position_gone_without_fills_is_manual_close(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "m1")
    ex.orders["m1-sl"] = OrderState("m1-sl", "open")
    ex.orders["m1-tp"] = OrderState("m1-tp", "open")
    ex.pos = []  # позиции на бирже нет
    mon.tick()
    assert trade(j, "m1")["status"] == "open" and ex.cancelled == []  # один тик — не приговор
    mon.tick()  # подтверждено вторым тиком
    t = trade(j, "m1")
    assert t["status"] == "closed" and t["close_reason"] == "manual"
    assert t["exit_price"] is None and t["realized_pnl"] is None and t["reconciled"] == 0
    assert set(ex.cancelled) == {"m1-sl", "m1-tp"}


def test_duration_stop_closes_only_old_losers(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path, max_loss_hold=3 * 3600)
    old = T0 - timedelta(hours=4)
    add_trade(j, "loser", symbol="A/USDT:USDT", opened_at=old)
    add_trade(j, "winner", symbol="B/USDT:USDT", opened_at=old)
    add_trade(j, "young", symbol="C/USDT:USDT", opened_at=T0 - timedelta(hours=1))
    for d in ("loser", "winner", "young"):
        ex.orders[f"{d}-sl"] = OrderState(f"{d}-sl", "open")
        ex.orders[f"{d}-tp"] = OrderState(f"{d}-tp", "open")
    ex.pos = [
        PositionInfo("A/USDT:USDT", "long", 2.0, 100.0, -3.0),
        PositionInfo("B/USDT:USDT", "long", 2.0, 100.0, +3.0),
        PositionInfo("C/USDT:USDT", "long", 2.0, 100.0, -3.0),
    ]
    rep = mon.tick()
    assert ex.closed == [("A/USDT:USDT", "long")]
    t = trade(j, "loser")
    assert t["close_reason"] == "duration_timeout" and t["exit_price"] == 97.0
    assert abs(t["realized_pnl"] - ((97.0 - 100.0) * 2.0 - 0.2)) < 1e-9
    assert trade(j, "winner")["status"] == "open" and trade(j, "young")["status"] == "open"
    assert rep.closed == [("loser", "duration_timeout")]


# ------------------------------------------------------------------- reconcile


def test_reconcile_reports_mismatch_once_and_clears(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    ex.pos = [PositionInfo("X/USDT:USDT", "short", 5.0, 10.0, 0.0)]  # в журнале такой нет
    mon.tick()
    assert notes == [] and not state.safe_mode  # один сомнительный ответ — не расхождение
    mon.tick()
    mon.tick()
    assert sum("reconcile" in n for n in notes) == 1  # подтверждено, но не спамим каждый тик
    assert state.safe_mode  # неизвестное состояние счета = новых входов нет
    ex.pos = []
    rep = mon.tick()
    assert rep.mismatches == []
    ex.pos = [PositionInfo("X/USDT:USDT", "short", 5.0, 10.0, 0.0)]
    mon.tick()
    mon.tick()
    assert sum("reconcile" in n for n in notes) == 2  # вернулось — сообщили снова


def test_qty_mismatch_within_tolerance_is_quiet(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "q1", qty=2.0)
    ex.orders["q1-sl"] = OrderState("q1-sl", "open")
    ex.orders["q1-tp"] = OrderState("q1-tp", "open")
    ex.pos = [PositionInfo(SYM, "long", 2.01, 100.0, 1.0)]  # округление биржи
    rep = mon.tick()
    assert rep.mismatches == [] and notes == []


# ---------------------------------------------------------------- resilience


def test_error_in_one_trade_does_not_block_others(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "bad", symbol="BAD/USDT:USDT")
    add_trade(j, "good", symbol="GOOD/USDT:USDT")
    ex.fail_symbols = {"BAD/USDT:USDT"}
    ex.orders["good-sl"] = OrderState("good-sl", "filled", 2.0, 98.0)
    ex.orders["good-tp"] = OrderState("good-tp", "open")
    rep = mon.tick()
    assert trade(j, "good")["status"] == "closed"
    assert trade(j, "bad")["status"] == "open"
    assert len(rep.errors) == 1 and "bad" in rep.errors[0]


def test_status_endpoint_exposes_trades_and_monitor(tmp_path):
    from fastapi.testclient import TestClient

    from oskar.core.api import build_app

    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "d1")
    ex.orders["d1-sl"] = OrderState("d1-sl", "open")
    ex.orders["d1-tp"] = OrderState("d1-tp", "open")
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 1.0)]
    mon.tick()
    payload = TestClient(build_app(state, monitor=mon)).get("/status").json()
    assert payload["trades"]["open"] == 1 and payload["trades"]["monitor"]["ticks"] == 1


# -------------------------------------------------------------------- adapters


def test_paper_adapter_order_lifecycle():
    px = [100.0]
    paper = PaperAdapter(lambda s: px[0])
    entry = paper.place_entry(SYM, "long", 2.0, "cid-1")
    sl = paper.place_sl(SYM, "long", 98.0, "cid-1-sl")
    tp = paper.place_tp(SYM, "long", 104.0, "cid-1-tp")
    assert paper.order_status(SYM, exchange_id=entry.exchange_id).is_filled
    assert paper.order_status(SYM, client_order_id="cid-1").avg_price == 100.0
    assert paper.order_status(SYM, exchange_id=sl.exchange_id).status == "open"

    paper.mark(SYM, high=101.0, low=97.5)  # стоп задет
    sl_state = paper.order_status(SYM, exchange_id=sl.exchange_id)
    assert sl_state.is_filled and sl_state.avg_price == 98.0
    assert paper.order_status(SYM, exchange_id=tp.exchange_id).is_gone
    assert paper.cancel_order(SYM, tp.exchange_id)  # идемпотентно
    assert paper.positions() == []


def test_bingx_adapter_order_status_mapping_and_fallbacks():
    from oskar.execution.bingx import BingXAdapter

    class OrderNotFound(Exception):
        pass

    class FakeCcxt:
        def __init__(self):
            self.cancelled = []

        def fetch_order(self, oid, symbol, params=None):
            if oid is None:
                raise Exception("clientOrderId lookup unsupported")  # noqa: TRY002
            return {"id": oid, "status": "closed", "filled": 2.0, "average": 98.0,
                    "fee": {"cost": 0.1}}

        def fetch_open_orders(self, symbol):
            return []

        def fetch_closed_orders(self, symbol):
            return [{"id": "9", "clientOrderId": "OSKAR-r1-x", "status": "canceled", "filled": 0}]

        def cancel_order(self, oid, symbol):
            if oid == "gone":
                raise OrderNotFound("already gone")
            self.cancelled.append(oid)

    ex = FakeCcxt()
    ad = BingXAdapter(ex)
    st = ad.order_status(SYM, exchange_id="5")
    assert st.is_filled and st.avg_price == 98.0 and st.fee == 0.1
    by_cid = ad.order_status(SYM, client_order_id="OSKAR-r1-x")
    assert by_cid.is_gone and by_cid.exchange_id == "9"
    assert ad.cancel_order(SYM, "5") and ex.cancelled == ["5"]
    assert ad.cancel_order(SYM, "gone")  # «уже нет» = успех


def test_orphan_oskar_orders_cancelled_known_and_foreign_untouched(tmp_path):
    """Ордер OSKAR без сделки в журнале снимается; свои ордера живой сделки и
    чужие (ручные, прикрепленные биржей) — нет. Скан — раз в ORPHAN_SCAN_SEC."""
    from oskar.core.position_monitor import ORPHAN_SCAN_SEC

    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "d1")
    ex.orders["d1-sl"] = OrderState("d1-sl", "open")
    ex.orders["d1-tp"] = OrderState("d1-tp", "open")
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 1.0)]
    ex.open_list = [
        OpenOrderRef(SYM, "d1-sl", "OSKAR-r1-d1-sl", "sl"),               # своя живая сделка
        OpenOrderRef(SYM, "x9", "oskar-r1-d1-sl-r2", "sl"),               # та же сделка (база cid)
        OpenOrderRef("A/USDT:USDT", "o1", "OSKAR-dead-abc123", "entry"),  # сирота-вход
        OpenOrderRef("B/USDT:USDT", "o2", "OSKAR-old-def456-tp", "tp"),   # остаток закрытой сделки
        OpenOrderRef("C/USDT:USDT", "m1", "manual-ivan", "entry"),         # ручной ордер Ивана
        OpenOrderRef("C/USDT:USDT", "a1", "", "sl"),                       # прикрепленный биржей
    ]
    rep = mon.tick()
    assert sorted(ex.cancelled) == ["o1", "o2"]
    assert sorted(rep.orphans) == ["A/USDT:USDT:entry:o1", "B/USDT:USDT:tp:o2"]
    assert any("сирота" in n for n in notes)

    ex.cancelled.clear()
    clock[0] = T0 + timedelta(seconds=ORPHAN_SCAN_SEC - 1)
    mon.tick()
    assert ex.cancelled == []  # до следующего скана не трогаем


def test_monitor_ticks_closed_trade_reconciler(tmp_path):
    """Сверка закрытых по филлам биржи вызывается из полного тика монитора."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    calls = []

    class Rec:
        def tick(self, now):
            calls.append(now)

    mon._reconciler = Rec()
    mon.tick()
    mon.tick_pending()  # быстрый проход сверку не дергает
    assert calls == [T0]
