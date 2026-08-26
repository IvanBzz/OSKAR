"""Execution: параметры BingX-ордеров против граблей v1, paper-механика."""

import pytest

from oskar.execution.bingx import BingXAdapter
from oskar.execution.paper import PaperAdapter


class FakeCcxt:
    """Захватывает вызовы create_order/set_leverage для проверки параметров."""

    def __init__(self):
        self.orders: list[dict] = []
        self.leverage_calls: list[dict] = []
        self._positions = []
        self._open_orders = []
        self.cancelled: list[str] = []

    def amount_to_precision(self, symbol, qty):
        return f"{qty:.4f}"

    def set_leverage(self, leverage, symbol, params=None):
        self.leverage_calls.append({"leverage": leverage, "symbol": symbol, "params": params})

    def create_order(self, symbol, type_, side, amount, price, params):
        order = {"symbol": symbol, "type": type_, "side": side, "amount": amount,
                 "price": price, "params": params, "id": str(len(self.orders) + 1)}
        self.orders.append(order)
        return {"id": order["id"], "status": "open"}

    def fetch_open_orders(self, symbol):
        return self._open_orders

    def cancel_order(self, order_id, symbol):
        self.cancelled.append(order_id)

    def fetch_positions(self, symbols=None):
        return self._positions

    def fetch_balance(self):
        return {"USDT": {"total": 100.0, "free": 90.0, "used": 10.0}}


@pytest.fixture
def fake():
    return FakeCcxt()


@pytest.fixture
def adapter(fake):
    return BingXAdapter(fake)


def test_entry_market_short_params(adapter, fake):
    adapter.place_entry("SOL-USDT", "short", 5.0, "OSKAR-r1-d1")
    o = fake.orders[0]
    assert o["symbol"] == "SOL/USDT:USDT"      # канонизация на границе
    assert o["type"] == "market" and o["side"] == "sell"
    assert o["params"]["positionSide"] == "SHORT"       # ОБЯЗАТЕЛЕН (109400)
    assert o["params"]["clientOrderId"] == "OSKAR-r1-d1"
    assert "reduceOnly" not in o["params"]              # ЗАПРЕЩЕН в hedge


def test_entry_limit_long(adapter, fake):
    adapter.place_entry("ETH/USDT:USDT", "long", 1.0, "OSKAR-r1-d2",
                        entry_type="limit", price=98.5)
    o = fake.orders[0]
    assert o["type"] == "limit" and o["side"] == "buy" and o["price"] == 98.5
    assert o["params"]["positionSide"] == "LONG"


def test_entry_limit_without_price_rejected(adapter):
    with pytest.raises(ValueError, match="positive price"):
        adapter.place_entry("SOL/USDT:USDT", "long", 1.0, "x", entry_type="limit")


def test_sl_tp_are_separate_closing_orders(adapter, fake):
    """BingX не принимает inline SL/TP — два отдельных ордера с closePosition."""
    adapter.place_sl("SOL/USDT:USDT", "short", 103.5, "OSKAR-r1-d1-sl")
    adapter.place_tp("SOL/USDT:USDT", "short", 95.0, "OSKAR-r1-d1-tp")
    sl, tp = fake.orders
    assert sl["type"] == "STOP_MARKET" and tp["type"] == "TAKE_PROFIT_MARKET"
    for o in (sl, tp):
        assert o["side"] == "buy"                       # закрытие SHORT = buy
        assert o["params"]["positionSide"] == "SHORT"
        assert o["params"]["closePosition"] is True
        assert o["params"]["stopPrice"] > 0
        assert o["amount"] is None                      # qty не нужен при closePosition
        assert "reduceOnly" not in o["params"]


def test_set_leverage_carries_side(adapter, fake):
    adapter.set_leverage("SOL/USDT:USDT", 6, "short")
    call = fake.leverage_calls[0]
    assert call["params"] == {"side": "SHORT"}          # BingX требует side


def test_normalize_qty_uses_exchange_precision(adapter):
    assert adapter.normalize_qty("SOLUSDT", 5.123456789) == 5.1235


def test_close_position_computes_qty_from_contracts(adapter, fake):
    fake._positions = [
        {"symbol": "SOL-USDT", "side": "short", "contracts": 7.0, "contractSize": 1.0,
         "entryPrice": 100.0, "unrealizedPnl": 3.0},
    ]
    placed = adapter.close_position("SOL/USDT:USDT", "short")
    assert placed is not None
    o = fake.orders[0]
    assert o["side"] == "buy" and o["amount"] == 7.0
    assert adapter.close_position("SOL/USDT:USDT", "long") is None  # нет позиции


def test_cancel_all_counts(adapter, fake):
    fake._open_orders = [{"id": "1"}, {"id": "2"}]
    assert adapter.cancel_all("SOL/USDT:USDT") == 2
    assert fake.cancelled == ["1", "2"]


# --------------------------------------------------------------------- paper


def make_paper(price=100.0):
    prices = {"SOL/USDT:USDT": price}
    return PaperAdapter(lambda s: prices[s], start_balance=1000.0), prices


def test_paper_entry_and_tp():
    paper, prices = make_paper()
    paper.place_entry("SOL/USDT:USDT", "short", 5.0, "OSKAR-p-1")
    paper.place_sl("SOL/USDT:USDT", "short", 103.0, "OSKAR-p-1-sl")
    paper.place_tp("SOL/USDT:USDT", "short", 96.0, "OSKAR-p-1-tp")

    fills = paper.mark("SOL/USDT:USDT", high=101.0, low=95.5)  # TP задет
    assert [f.reason for f in fills] == ["tp_hit"]
    assert paper.balance().total == pytest.approx(1000.0 + (100.0 - 96.0) * 5.0)
    assert paper.positions() == []


def test_paper_sl_wins_collision():
    """Бар зацепил и SL и TP — SL первым (консервативно, как в реплее)."""
    paper, _ = make_paper()
    paper.place_entry("SOL/USDT:USDT", "long", 2.0, "OSKAR-p-2")
    paper.place_sl("SOL/USDT:USDT", "long", 98.0, "sl")
    paper.place_tp("SOL/USDT:USDT", "long", 104.0, "tp")
    fills = paper.mark("SOL/USDT:USDT", high=105.0, low=97.0)
    assert [f.reason for f in fills] == ["sl_hit"]
    assert paper.balance().total == pytest.approx(1000.0 - 2.0 * 2.0)


def test_paper_duplicate_position_rejected():
    paper, _ = make_paper()
    paper.place_entry("SOL/USDT:USDT", "long", 1.0, "a")
    with pytest.raises(RuntimeError, match="duplicate"):
        paper.place_entry("SOL-USDT", "long", 1.0, "b")  # тот же канон
