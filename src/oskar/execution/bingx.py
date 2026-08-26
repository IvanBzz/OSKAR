"""BingXAdapter — исполнение через ccxt в hedge mode. Все грабли v1 зашиты:

- positionSide (LONG/SHORT) ОБЯЗАТЕЛЕН в каждом ордере (code 109400 без него)
- reduceOnly ЗАПРЕЩЕН в hedge mode (code 109400) — ключ не передается никогда
- SL/TP только ОТДЕЛЬНЫМИ ордерами STOP_MARKET / TAKE_PROFIT_MARKET
  с stopPrice и closePosition=True (inline SL/TP биржа не принимает)
- set_leverage требует params={'side': 'LONG'|'SHORT'}
- каждый ордер несет clientOrderId (реконсиляция, ст. 1)

ccxt-объект инжектится; тесты проверяют построение параметров на фейке.
"""

from typing import Any

from oskar.data.symbols import canonical
from oskar.execution.base import (
    BalanceInfo,
    ExchangeAdapter,
    PlacedOrder,
    PositionInfo,
    Side,
)

_POSITION_SIDE = {"long": "LONG", "short": "SHORT"}
_ENTRY_ORDER_SIDE = {"long": "buy", "short": "sell"}
_CLOSE_ORDER_SIDE = {"long": "sell", "short": "buy"}

FORBIDDEN_PARAMS = ("reduceOnly",)  # hedge mode BingX не принимает


class BingXAdapter(ExchangeAdapter):
    def __init__(self, exchange: Any):
        self._ex = exchange

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _guard_params(params: dict) -> dict:
        for k in FORBIDDEN_PARAMS:
            if k in params:
                raise ValueError(f"param '{k}' is forbidden in BingX hedge mode")
        if "positionSide" not in params:
            raise ValueError("positionSide is mandatory in BingX hedge mode")
        return params

    @staticmethod
    def _placed(raw: dict, client_order_id: str, order_type: str) -> PlacedOrder:
        return PlacedOrder(
            exchange_id=str(raw.get("id", "")),
            client_order_id=client_order_id,
            type=order_type,
            status=str(raw.get("status", "open")),
        )

    # ------------------------------------------------------------ contract

    def normalize_qty(self, symbol: str, qty: float) -> float:
        sym = canonical(symbol)
        return float(self._ex.amount_to_precision(sym, qty))

    def set_leverage(self, symbol: str, leverage: float, side: Side) -> None:
        self._ex.set_leverage(
            leverage, canonical(symbol), params={"side": _POSITION_SIDE[side]}
        )

    def place_entry(
        self,
        symbol: str,
        side: Side,
        qty: float,
        client_order_id: str,
        *,
        entry_type: str = "market",
        price: float | None = None,
    ) -> PlacedOrder:
        if entry_type == "limit" and (price is None or price <= 0):
            raise ValueError("limit entry requires positive price")
        params = self._guard_params({
            "positionSide": _POSITION_SIDE[side],
            "clientOrderId": client_order_id,
        })
        raw = self._ex.create_order(
            canonical(symbol),
            entry_type,
            _ENTRY_ORDER_SIDE[side],
            qty,
            price if entry_type == "limit" else None,
            params,
        )
        return self._placed(raw, client_order_id, entry_type.upper())

    def _place_protective(
        self,
        symbol: str,
        side: Side,
        stop_price: float,
        client_order_id: str,
        order_type: str,
    ) -> PlacedOrder:
        if stop_price <= 0:
            raise ValueError("stop_price must be positive")
        params = self._guard_params({
            "positionSide": _POSITION_SIDE[side],
            "stopPrice": stop_price,
            "closePosition": True,
            "clientOrderId": client_order_id,
        })
        raw = self._ex.create_order(
            canonical(symbol),
            order_type,
            _CLOSE_ORDER_SIDE[side],  # закрывающая сторона
            None,  # qty не нужен при closePosition
            None,
            params,
        )
        return self._placed(raw, client_order_id, order_type)

    def place_sl(
        self, symbol: str, side: Side, stop_price: float, client_order_id: str
    ) -> PlacedOrder:
        return self._place_protective(symbol, side, stop_price, client_order_id, "STOP_MARKET")

    def place_tp(
        self, symbol: str, side: Side, stop_price: float, client_order_id: str
    ) -> PlacedOrder:
        return self._place_protective(
            symbol, side, stop_price, client_order_id, "TAKE_PROFIT_MARKET"
        )

    def cancel_all(self, symbol: str) -> int:
        sym = canonical(symbol)
        orders = self._ex.fetch_open_orders(sym)
        for o in orders:
            self._ex.cancel_order(o["id"], sym)
        return len(orders)

    def close_position(self, symbol: str, side: Side) -> PlacedOrder | None:
        sym = canonical(symbol)
        pos_qty = None
        for p in self._ex.fetch_positions([sym]):
            if str(p.get("side", "")).lower() == side:
                contracts = float(p.get("contracts") or 0.0)
                size = float(p.get("contractSize") or 1.0)
                if contracts > 0:
                    pos_qty = contracts * size
        if pos_qty is None:
            return None
        params = self._guard_params({"positionSide": _POSITION_SIDE[side]})
        raw = self._ex.create_order(
            sym, "market", _CLOSE_ORDER_SIDE[side], pos_qty, None, params
        )
        return self._placed(raw, "", "MARKET")

    def positions(self) -> list[PositionInfo]:
        out = []
        for p in self._ex.fetch_positions():
            contracts = float(p.get("contracts") or 0.0)
            if contracts == 0:
                continue
            size = float(p.get("contractSize") or 1.0)
            out.append(
                PositionInfo(
                    symbol=canonical(p["symbol"]),
                    side=str(p.get("side", "")).lower(),  # type: ignore[arg-type]
                    qty=contracts * size,
                    entry_price=float(p.get("entryPrice") or 0.0),
                    unrealized_pnl=float(p.get("unrealizedPnl") or 0.0),
                )
            )
        return out

    def balance(self) -> BalanceInfo:
        raw = self._ex.fetch_balance()
        usdt = raw.get("USDT", {})
        return BalanceInfo(
            total=float(usdt.get("total") or 0.0),
            free=float(usdt.get("free") or 0.0),
            used=float(usdt.get("used") or 0.0),
        )
