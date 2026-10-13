"""Находки Claudexor deep-scan (4 Claude-эксплорера), не покрытые остальными ревью."""

import httpx

from oskar.core.state import RuntimeState
from oskar.core.telegram_buttons import ButtonPoller
from oskar.execution.bingx import BingXAdapter
from oskar.journal.db import connect
from tests.unit.test_buttons import CHAT, FakeBotApi, add_pending, callback_update


def test_market_button_without_executor_does_not_claim_an_order(tmp_path):
    """Без ключей биржи кнопка писала «Беру по рынку ⚡» — ордера не было."""
    j = connect(tmp_path / "j.db")
    add_pending(j, "d1")
    state = RuntimeState(journal=j)
    fake = FakeBotApi([callback_update("oskar:market:d1")])
    ButtonPoller("TOKEN", CHAT, state, client=httpx.Client(transport=fake)).poll_once(timeout=0)
    answer = next(b for p, b in fake.calls if p == "answerCallbackQuery")
    assert "НЕ поставлен" in answer["text"] and "рынку" not in answer["text"]
    row = j.execute("SELECT resolution FROM pending_approvals WHERE decision_id='d1'").fetchone()
    assert row["resolution"] == "approved"  # решение записано честно, как approve без исполнения


def test_protective_stop_price_rounded_to_tick():
    """stopPrice в params обходил price_to_precision ccxt — сырой float отвергался по тик-сайзу."""

    class Ex:
        markets = {"SOL/USDT:USDT": {"precision": {"price": 2}}}

        def __init__(self):
            self.orders = []

        def price_to_precision(self, symbol, price):
            return f"{price:.2f}"

        def create_order(self, symbol, type_, side, amount, price, params):
            self.orders.append((symbol, type_, side, amount, price, params))
            return {"id": "1", "status": "open"}

    ex = Ex()
    BingXAdapter(ex).place_sl("SOL/USDT:USDT", "long", 97.123456, "cid-sl")
    assert ex.orders[0][5]["stopPrice"] == 97.12


def test_cancel_order_with_empty_id_is_not_success():
    class Ex:
        def cancel_order(self, oid, symbol):  # pragma: no cover - не должен вызываться
            raise AssertionError("must not be called with empty id")

    assert BingXAdapter(Ex()).cancel_order("SOL/USDT:USDT", "") is False
