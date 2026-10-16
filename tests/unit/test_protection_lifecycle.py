"""Защита входа (живой случай 2026-10-14, лимитка BERA: 109420 «position not exist»).

BingX отвергает отдельные closePosition-ордера SL/TP, пока позиции нет. Теперь
SL/TP прикрепляются к самому входу; если биржа не приняла прикрепление — голый
вход + защита по факту (маркет: ждем позицию, лимит: монитор при исполнении).
"""

from dataclasses import replace
from datetime import timedelta

from oskar.contracts.schemas import ExecutionPlan
from oskar.core.executor import ExecutionService
from oskar.execution.base import (
    BalanceInfo,
    ExchangeAdapter,
    OrderNotFoundError,
    OrderState,
    PlacedOrder,
    PositionInfo,
    ProtectiveOrder,
    pick_protection,
)
from oskar.journal.db import SCHEMA_VERSION, connect
from tests.unit.test_buttons import add_pending
from tests.unit.test_position_monitor import SYM, T0, add_trade, make_monitor, trade


class ExchangeError(Exception):
    """Имитация ccxt.ExchangeError — биржа ОТВЕТИЛА отказом."""


class RequestTimeout(Exception):
    """Имитация ccxt.RequestTimeout (подкласс NetworkError) — исход неизвестен."""


RequestTimeout.__mro__  # noqa: B018 - classify_error смотрит имена классов в MRO


class NetworkError(Exception):
    pass


class Timeout(NetworkError):
    """MRO содержит NetworkError — classify_error видит «network»."""


class ScriptedExchange(ExchangeAdapter):
    """Биржа по сценарию для исполнителя: записывает вызовы, отказывает по заказу."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.reject_attached: Exception | None = None  # отказ входа С прикреплением
        self.entry_error: Exception | None = None  # отказ любого входа
        self.leverage_errors: list[Exception] = []
        self.sl_errors: list[Exception] = []
        self.position_after_polls = 0  # позиция видна после N опросов
        self._polls = 0
        self.orders: dict[str, OrderState] = {}
        self.by_cid: dict[str, OrderState] = {}
        self.protection: list[ProtectiveOrder] = []
        self.pos: list[PositionInfo] = []
        self.closed_qty: list[tuple[str, str, float]] = []  # close_qty: (symbol, side, qty)

    def normalize_qty(self, symbol, qty):
        return round(qty, 4)

    def set_leverage(self, symbol, leverage, side):
        self.calls.append(("leverage", leverage))
        if self.leverage_errors:
            raise self.leverage_errors.pop(0)

    def place_entry(self, symbol, side, qty, cid, *, entry_type="market", price=None,
                    stop_loss=None, take_profit=None):
        self.calls.append(("entry", entry_type, stop_loss, take_profit))
        if stop_loss is not None and self.reject_attached is not None:
            raise self.reject_attached
        if self.entry_error is not None:
            raise self.entry_error
        oid = f"e{len(self.calls)}"
        filled = entry_type == "market"
        self.orders[oid] = OrderState(oid, "filled" if filled else "open", qty if filled else 0.0,
                                      100.0 if filled else None, 0.05 if filled else None)
        if filled:
            self.pos = [PositionInfo(symbol, side, qty, 100.0, 0.0)]
            if stop_loss is not None:  # биржа создает прикрепленные SL/TP при исполнении
                # у прикрепленных SL/TP BingX объем = объему входа (ccxt amount)
                self.protection = [ProtectiveOrder("att-sl", "sl", stop_loss, "open", qty=qty),
                                   ProtectiveOrder("att-tp", "tp", take_profit, "open", qty=qty)]
        return PlacedOrder(oid, cid, entry_type.upper(), "open")

    def place_sl(self, symbol, side, stop_price, cid):
        self.calls.append(("sl", stop_price, cid))
        if self.sl_errors:
            raise self.sl_errors.pop(0)
        return PlacedOrder("sl-1", cid, "STOP_MARKET", "open")

    def place_tp(self, symbol, side, stop_price, cid):
        self.calls.append(("tp", stop_price, cid))
        return PlacedOrder("tp-1", cid, "TAKE_PROFIT_MARKET", "open")

    def cancel_all(self, symbol):
        return 0

    def close_position(self, symbol, side):
        self.calls.append(("close", symbol, side))
        self.pos = []
        return PlacedOrder("c1", "", "MARKET", "closed")

    def close_qty(self, symbol, side, qty):
        """Закрытие ровно qty (panic): позиция уменьшается, ручной остаток той же пары живет."""
        match = [p for p in self.pos if p.symbol == symbol and p.side == side]
        if not match or qty <= 0:
            return None
        done = min(qty, sum(p.qty for p in match))
        self.calls.append(("close", symbol, side))
        self.closed_qty.append((symbol, side, done))
        rest = round(sum(p.qty for p in match) - done, 10)
        self.pos = [p for p in self.pos if p not in match]
        if rest > 0:
            self.pos.append(replace(match[0], qty=rest))
        return PlacedOrder("c1", "", "MARKET", "closed")

    def positions(self):
        self._polls += 1
        return list(self.pos) if self._polls > self.position_after_polls else []

    def balance(self):
        return BalanceInfo(121.71, 121.71, 0.0)

    def order_status(self, symbol, exchange_id="", client_order_id=""):
        book, key = (self.orders, exchange_id) if exchange_id else (self.by_cid, client_order_id)
        if key not in book:
            raise OrderNotFoundError(key)
        return book[key]

    def cancel_order(self, symbol, exchange_id):
        self.calls.append(("cancel", exchange_id))
        return True

    def protection_orders(self, symbol, side, *, include_closed=False):
        return [o for o in self.protection if include_closed or o.status == "open"]

    def open_orders_all(self):
        return []


def plan(**overrides) -> ExecutionPlan:
    base = dict(
        candidate_id="BERAUSDTUSDT-1791967500000-long", symbol="BERA/USDT:USDT", side="long",
        entry_market=0.2300, entry_limit=0.2292, entry_limit_note="FVG",
        sl=0.2276, tp=0.2346, qty=778.41, leverage=3.0, risk_usd=1.217, rr_fee_adjusted=2.4,
        timeframe="15m", tldr="Ретест FVG после свипа; цель — хай диапазона.",
    )
    base.update(overrides)
    return ExecutionPlan(**base)


def make_service(tmp_path, ex, p=None):
    p = p or plan()
    j = connect(tmp_path / "j.db")
    add_pending(j, p.candidate_id)
    j.execute("UPDATE pending_approvals SET card_json=? WHERE decision_id=?",
              (p.model_dump_json(), p.candidate_id))
    j.execute("INSERT INTO decisions (run_id, decision_id, candidate_json, outcome) "
              "VALUES ('r1', ?, '{}', 'pending')", (p.candidate_id,))
    j.commit()
    sleeps: list[float] = []
    return ExecutionService(ex, j, "r1", sleep=sleeps.append), j, p, sleeps


def row(j, p):
    return j.execute("SELECT * FROM trades WHERE decision_id=?", (p.candidate_id,)).fetchone()


# ------------------------------------------------------------------- executor


def test_limit_entry_carries_attached_protection_no_standalone_orders(tmp_path):
    """Ровно живой случай BERA: лимитка — SL/TP прикреплены к входу, отдельных
    closePosition-ордеров до исполнения НЕТ (их BingX и отвергал 109420)."""
    ex = ScriptedExchange()
    svc, j, p, _ = make_service(tmp_path, ex)
    out = svc.execute(p.candidate_id, "limit")
    assert out.ok, out.message
    assert ("entry", "limit", p.sl, p.tp) in ex.calls
    assert not any(c[0] in ("sl", "tp", "cancel", "close") for c in ex.calls)
    r = row(j, p)
    assert r["status"] == "pending" and r["entry_type"] == "limit" and r["entry_order_id"]
    assert r["sl_order_id"] is None and "встанут при исполнении" in out.message


def test_market_attached_protection_adopted_immediately(tmp_path):
    ex = ScriptedExchange()
    svc, j, p, _ = make_service(tmp_path, ex)
    out = svc.execute(p.candidate_id, "market")
    assert out.ok, out.message
    r = row(j, p)
    assert r["status"] == "open" and r["entry_price"] == 100.0 and r["entry_fees"] == 0.05
    assert (r["sl_order_id"], r["tp_order_id"]) == ("att-sl", "att-tp")
    assert not any(c[0] in ("sl", "tp") for c in ex.calls)


def test_rejected_attachment_falls_back_to_bare_market_and_waits_for_position(tmp_path):
    """Биржа не приняла прикрепление -> голый маркет; позиция видна не сразу, SL
    первый раз ловит 109420 — повтор, а не «закрыть позицию»."""
    ex = ScriptedExchange()
    ex.reject_attached = ExchangeError('bingx {"code":80001,"msg":"stopLoss not supported"}')
    ex.position_after_polls = 2
    ex.sl_errors = [ExchangeError('bingx {"code":109420,"msg":"position not exist","data":{}}')]
    svc, j, p, sleeps = make_service(tmp_path, ex)
    out = svc.execute(p.candidate_id, "market")
    assert out.ok, out.message
    entries = [c for c in ex.calls if c[0] == "entry"]
    assert entries == [("entry", "market", p.sl, p.tp), ("entry", "market", None, None)]
    assert [c[0] for c in ex.calls if c[0] in ("sl", "tp")] == ["sl", "sl", "tp"]
    assert not any(c[0] == "close" for c in ex.calls)
    r = row(j, p)
    assert r["status"] == "open" and r["sl_order_id"] == "sl-1" and r["tp_order_id"] == "tp-1"
    assert sleeps  # ждали позицию / повторяли SL


def test_rejected_attachment_bare_limit_left_to_monitor(tmp_path):
    ex = ScriptedExchange()
    ex.reject_attached = ExchangeError('bingx {"code":80001,"msg":"bad param"}')
    svc, j, p, _ = make_service(tmp_path, ex)
    out = svc.execute(p.candidate_id, "limit")
    assert out.ok and "поставит монитор" in out.message
    assert not any(c[0] in ("sl", "tp") for c in ex.calls)
    assert row(j, p)["status"] == "pending"


def test_market_stop_never_placed_closes_position(tmp_path):
    ex = ScriptedExchange()
    ex.reject_attached = ExchangeError('bingx {"code":80001,"msg":"bad param"}')
    ex.sl_errors = [ExchangeError('bingx {"code":80014,"msg":"stop price invalid"}')]
    svc, j, p, _ = make_service(tmp_path, ex)
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and out.entry_sent and "стоп не встал" in out.message
    assert ("close", p.symbol, p.side) in ex.calls
    r = row(j, p)
    assert r["status"] == "closed" and r["close_reason"] == "manual"


def test_entry_timeout_found_by_client_id_is_tracked(tmp_path):
    ex = ScriptedExchange()
    ex.entry_error = Timeout("bingx POST .../trade/order timed out")
    svc, j, p, _ = make_service(tmp_path, ex)
    from oskar.core.executor import make_client_order_id

    cid = make_client_order_id("r1", p.candidate_id)
    ex.by_cid[cid] = OrderState("e-late", "open")  # биржа ордер приняла, ответ потерялся
    out = svc.execute(p.candidate_id, "limit")
    assert out.ok, out.message
    r = row(j, p)
    assert r["entry_order_id"] == "e-late" and r["status"] == "pending"


def test_entry_timeout_unknown_records_intent_and_blocks_repress(tmp_path):
    ex = ScriptedExchange()
    ex.entry_error = Timeout("timed out")
    svc, j, p, _ = make_service(tmp_path, ex)
    out = svc.execute(p.candidate_id, "market")
    assert not out.ok and out.entry_sent and "НЕ нажимай" in out.message
    r = row(j, p)
    assert r["status"] == "pending" and r["entry_order_id"] is None and r["entry_type"] == "market"
    assert svc.execute(p.candidate_id, "market").message == "уже исполнено"


def test_leverage_timeout_is_retried_not_failed(tmp_path):
    """Живой случай 2026-10-14: первое нажатие умерло на таймауте set_leverage."""
    ex = ScriptedExchange()
    ex.leverage_errors = [Timeout("read operation timed out")]
    svc, j, p, sleeps = make_service(tmp_path, ex)
    out = svc.execute(p.candidate_id, "limit")
    assert out.ok, out.message
    assert [c for c in ex.calls if c[0] == "leverage"] == [("leverage", 3.0), ("leverage", 3.0)]


# -------------------------------------------------------------------- monitor


def test_monitor_adopts_attached_protection_after_limit_fill(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "p1", status="pending", opened_at=T0 - timedelta(minutes=5))
    j.execute("UPDATE trades SET sl_order_id=NULL, tp_order_id=NULL, entry_order_id='e1', "
              "entry_type='limit' WHERE decision_id='p1'")
    j.commit()
    ex.orders["e1"] = OrderState("e1", "filled", 2.0, 99.5, 0.03)
    ex.protection = [ProtectiveOrder("x-sl", "sl", 98.0, "open", qty=2.0),
                     ProtectiveOrder("x-tp", "tp", 104.0, "open", qty=2.0)]
    rep = mon.tick_pending()
    t = trade(j, "p1")
    assert t["status"] == "open" and t["entry_price"] == 99.5
    assert (t["sl_order_id"], t["tp_order_id"]) == ("x-sl", "x-tp")
    assert sorted(rep.adopted) == ["p1:sl", "p1:tp"] and rep.replaced == []


def test_monitor_places_protection_when_none_on_exchange(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "p1", status="pending", opened_at=T0 - timedelta(minutes=5))
    j.execute("UPDATE trades SET sl_order_id=NULL, tp_order_id=NULL, entry_order_id='e1' "
              "WHERE decision_id='p1'")
    j.commit()
    ex.orders["e1"] = OrderState("e1", "filled", 2.0, 99.5)
    placed = []
    ex.place_sl = lambda s, side, px, cid: (
        placed.append(("sl", px)) or PlacedOrder("s1", cid, "STOP_MARKET", "open"))
    ex.place_tp = lambda s, side, px, cid: (
        placed.append(("tp", px)) or PlacedOrder("t1", cid, "TAKE_PROFIT_MARKET", "open"))
    mon.tick_pending()
    assert placed == [("sl", 98.0), ("tp", 104.0)]
    t = trade(j, "p1")
    assert (t["sl_order_id"], t["tp_order_id"]) == ("s1", "t1")
    assert any("SL выставлен" in n for n in notes) and not state.safe_mode


def test_monitor_retries_position_missing_before_safe_mode(tmp_path):
    from oskar.core.position_monitor import PROTECT_RETRIES

    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "d1")
    j.execute("UPDATE trades SET sl_order_id=NULL WHERE decision_id='d1'")
    j.commit()
    ex.orders["d1-tp"] = OrderState("d1-tp", "open")
    ex.pos = [PositionInfo(SYM, "long", 2.0, 100.0, 0.5)]
    err = ExchangeError('bingx {"code":109420,"msg":"position not exist","data":{}}')
    ex.place_sl = lambda *a, **k: (_ for _ in ()).throw(err)
    for _ in range(PROTECT_RETRIES - 1):
        mon.tick()
        assert not state.safe_mode  # позиция еще не видна бирже — повтор, не паника
    mon.tick()
    assert state.safe_mode and any("БЕЗ SL" in n for n in notes)


def test_vanished_position_classified_by_exchange_history(tmp_path):
    """Позиция исчезла, id прикрепленного SL журнал не знал — история биржи
    показывает исполненный стоп: sl_hit с ценой, а не «manual»."""
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "d1", opened_at=T0 - timedelta(minutes=30))
    j.execute("UPDATE trades SET sl_order_id=NULL, tp_order_id=NULL WHERE decision_id='d1'")
    j.commit()
    opened_ms = int((T0 - timedelta(minutes=30)).timestamp() * 1000)
    ex.protection = [
        ProtectiveOrder("h-sl", "sl", 98.0, "filled", avg_price=97.9, fee=0.1,
                        timestamp_ms=opened_ms + 60_000, qty=2.0),
        ProtectiveOrder("h-tp", "tp", 104.0, "open", qty=2.0),
        ProtectiveOrder("old-sl", "sl", 98.0, "filled", avg_price=50.0, timestamp_ms=opened_ms - 10**7,
                        qty=2.0),
    ]
    ex.pos = []
    mon.tick()
    mon.tick()
    t = trade(j, "d1")
    assert t["close_reason"] == "sl_hit" and t["exit_price"] == 97.9
    assert "h-tp" in ex.cancelled  # парный TP снят


def test_tick_pending_does_not_touch_open_trades(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "o1")
    ex.pos = []  # позиции нет — полный тик начал бы отсчет «manual»
    mon.tick_pending()
    mon.tick_pending()
    assert trade(j, "o1")["status"] == "open"


def test_unknown_market_entry_expires_fast(tmp_path):
    from oskar.core.position_monitor import UNKNOWN_ENTRY_TTL_SEC

    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "u1", status="pending", opened_at=T0 - timedelta(seconds=UNKNOWN_ENTRY_TTL_SEC + 5))
    j.execute("UPDATE trades SET entry_type='market', sl_order_id=NULL, tp_order_id=NULL "
              "WHERE decision_id='u1'")
    j.commit()
    mon.tick_pending()
    assert trade(j, "u1")["status"] == "cancelled"


def test_pending_lookup_by_entry_order_id_not_client_id(tmp_path):
    mon, j, ex, clock, notes, _, state = make_monitor(tmp_path)
    add_trade(j, "p2", status="pending")
    j.execute("UPDATE trades SET entry_order_id='e9' WHERE decision_id='p2'")
    j.commit()
    ex.orders["e9"] = OrderState("e9", "open")  # by_cid пуст: поиск по cid упал бы
    rep = mon.tick_pending()
    assert rep.errors == [] and trade(j, "p2")["status"] == "pending"


# -------------------------------------------------------------------- adapters


def test_bingx_attached_params_and_protection_parsing():
    from oskar.execution.bingx import BingXAdapter

    class Ccxt:
        markets = {"BERA/USDT:USDT": {}}

        def __init__(self):
            self.created = []

        def create_order(self, symbol, type_, side, amount, price, params):
            self.created.append(params)
            return {"id": "1", "status": "open"}

        def fetch_open_orders(self, symbol):
            return [
                {"id": "10", "type": "market", "stopLossPrice": 0.2276, "status": "open",
                 "info": {"type": "STOP_MARKET", "positionSide": "LONG"}},
                {"id": "11", "type": "market", "takeProfitPrice": 0.2346, "status": "open",
                 "info": {"type": "TAKE_PROFIT_MARKET", "positionSide": "LONG"}},
                {"id": "12", "type": "market", "stopLossPrice": 0.30, "status": "open",
                 "info": {"type": "STOP_MARKET", "positionSide": "SHORT"}},  # чужая сторона
                {"id": "13", "type": "limit", "stopLossPrice": 0.2276, "status": "open",
                 "info": {"type": "LIMIT", "positionSide": "LONG"}},  # сам вход с прикреплением
            ]

        def fetch_canceled_and_closed_orders(self, symbol):
            return [{"id": "20", "type": "market", "stopLossPrice": 0.2276, "status": "closed",
                     "average": 0.2275, "lastTradeTimestamp": 1791968000000,
                     "info": {"type": "STOP_MARKET", "positionSide": "LONG"}}]

    ccxt = Ccxt()
    ad = BingXAdapter(ccxt)
    ad.place_entry("BERA/USDT:USDT", "long", 778.4, "cid", entry_type="limit", price=0.2292,
                   stop_loss=0.2276, take_profit=0.2346)
    params = ccxt.created[0]
    assert params["stopLoss"] == {"type": "STOP_MARKET", "triggerPrice": 0.2276, "workingType": "MARK_PRICE"}
    assert params["takeProfit"]["type"] == "TAKE_PROFIT_MARKET" and params["positionSide"] == "LONG"
    assert "reduceOnly" not in params

    open_prot = ad.protection_orders("BERA/USDT:USDT", "long")
    assert [(o.exchange_id, o.kind, o.trigger_price) for o in open_prot] == [
        ("10", "sl", 0.2276), ("11", "tp", 0.2346)]
    hist = ad.protection_orders("BERA/USDT:USDT", "long", include_closed=True)
    filled = [o for o in hist if o.status == "filled"]
    assert len(filled) == 1 and filled[0].avg_price == 0.2275 and filled[0].timestamp_ms == 1791968000000


def test_bingx_client_id_lookup_case_insensitive_and_not_found():
    import pytest

    from oskar.execution.bingx import BingXAdapter

    class Ccxt:
        def fetch_open_orders(self, symbol):
            return []

        def fetch_canceled_and_closed_orders(self, symbol):
            return [{"id": "7", "clientOrderId": "oskar-r1-abc", "status": "canceled"}]

        def fetch_order(self, oid, symbol):
            class OrderNotFound(Exception):
                pass
            raise OrderNotFound("bingx order not exist")

    ad = BingXAdapter(Ccxt())
    assert ad.order_status("X/USDT:USDT", client_order_id="OSKAR-r1-abc").is_gone
    with pytest.raises(OrderNotFoundError):
        ad.order_status("X/USDT:USDT", client_order_id="OSKAR-r1-zzz")
    with pytest.raises(OrderNotFoundError):
        ad.order_status("X/USDT:USDT", exchange_id="404")


def test_pick_protection_ignores_far_triggers():
    orders = [ProtectiveOrder("a", "sl", 90.0, "open"), ProtectiveOrder("b", "sl", 98.01, "open"),
              ProtectiveOrder("c", "tp", 98.0, "open")]
    assert pick_protection(orders, "sl", 98.0).exchange_id == "b"
    assert pick_protection(orders, "sl", 120.0) is None


# -------------------------------------------------------------------- journal


def test_journal_migrates_v2_to_v3_without_losing_data(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    j = connect(db)
    add_trade(j, "keep")
    j.commit()
    j.close()
    # откатываем файл к v2: без новых колонок и с версией 2
    raw = sqlite3.connect(db)
    raw.execute("ALTER TABLE trades DROP COLUMN entry_order_id")
    raw.execute("ALTER TABLE trades DROP COLUMN entry_type")
    raw.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    raw.commit()
    raw.close()

    j = connect(db)
    cols = {r["name"] for r in j.execute("PRAGMA table_info(trades)")}
    assert {"entry_order_id", "entry_type"} <= cols
    assert j.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
    assert j.execute("SELECT decision_id FROM trades").fetchone()[0] == "keep"  # данные целы
