"""BingXAdapter — исполнение через ccxt в hedge mode. Все грабли v1 зашиты:

- positionSide (LONG/SHORT) ОБЯЗАТЕЛЕН в каждом ордере (code 109400 без него)
- reduceOnly ЗАПРЕЩЕН в hedge mode (code 109400) — ключ не передается никогда
- SL/TP только ОТДЕЛЬНЫМИ ордерами STOP_MARKET / TAKE_PROFIT_MARKET
  с stopPrice и closePosition=True (inline SL/TP биржа не принимает)
- set_leverage требует params={'side': 'LONG'|'SHORT'}
- каждый ордер несет clientOrderId (реконсиляция, ст. 1)

ccxt-объект инжектится; тесты проверяют построение параметров на фейке.
"""

import math
from typing import Any

from oskar.data.symbols import canonical
from oskar.execution.base import (
    BalanceInfo,
    ExchangeAdapter,
    OrderState,
    PlacedOrder,
    PositionInfo,
    Side,
)

# ccxt unified status -> OrderState.status
_ORDER_STATUS = {
    "open": "open", "closed": "filled", "filled": "filled",
    "canceled": "canceled", "cancelled": "canceled",
    "expired": "expired", "rejected": "rejected",
}

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

    def _ensure_markets(self) -> None:
        # свежий ccxt без load_markets падает на amount_to_precision («markets not
        # loaded») — первое же нажатие кнопки умирало бы (поймано ревью Codex 2026-10-13)
        if not getattr(self._ex, "markets", None) and hasattr(self._ex, "load_markets"):
            self._ex.load_markets()

    def normalize_qty(self, symbol: str, qty: float) -> float:
        self._ensure_markets()
        sym = canonical(symbol)
        return float(self._ex.amount_to_precision(sym, qty))

    def set_leverage(self, symbol: str, leverage: float, side: Side) -> None:
        # BingX принимает целое плечо; лестница дает дробное (4.4x) — округляем
        # ВНИЗ (меньше плечо = больше маржа = безопаснее), минимум 1x
        lev = max(1, int(math.floor(leverage)))
        self._ex.set_leverage(lev, canonical(symbol), params={"side": _POSITION_SIDE[side]})

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
        sym = canonical(symbol)
        # stopPrice в params уходит мимо price_to_precision ccxt (extend перетирает
        # округленное значение сырым float) -> отказ по тик-сайзу -> вход без защиты.
        # Округляем сами (Claudexor deep-scan 2026-10-13).
        if hasattr(self._ex, "price_to_precision"):
            self._ensure_markets()
            stop_price = float(self._ex.price_to_precision(sym, stop_price))
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
        # VST-демо держит средства в валюте 'VST', реал — в USDT
        entry = raw.get("USDT") or raw.get("VST") or {}
        return BalanceInfo(
            total=float(entry.get("total") or 0.0),
            free=float(entry.get("free") or 0.0),
            used=float(entry.get("used") or 0.0),
        )

    # ------------------------------------------------------- order lifecycle

    @staticmethod
    def _order_state(raw: dict) -> OrderState:
        status = str(raw.get("status") or "").lower()
        fee = raw.get("fee") or {}
        fee_cost = fee.get("cost") if isinstance(fee, dict) else None
        avg = raw.get("average")
        return OrderState(
            exchange_id=str(raw.get("id") or ""),
            status=_ORDER_STATUS.get(status, status or "unknown"),
            filled_qty=float(raw.get("filled") or 0.0),
            avg_price=float(avg) if avg else None,
            fee=float(fee_cost) if fee_cost is not None else None,
        )

    def _find_by_client_id(self, sym: str, client_order_id: str) -> dict:
        try:
            return self._ex.fetch_order(None, sym, {"clientOrderId": client_order_id})
        except Exception:  # noqa: BLE001 - не все версии ccxt ищут по clientOrderId
            pass
        for fetch in (self._ex.fetch_open_orders, self._ex.fetch_closed_orders):
            for o in fetch(sym):
                if str(o.get("clientOrderId") or "") == client_order_id:
                    return o
        raise RuntimeError(f"order {client_order_id} not found on exchange for {sym}")

    def order_status(
        self, symbol: str, exchange_id: str = "", client_order_id: str = ""
    ) -> OrderState:
        if not exchange_id and not client_order_id:
            raise ValueError("order_status needs exchange_id or client_order_id")
        sym = canonical(symbol)
        raw = (
            self._ex.fetch_order(exchange_id, sym)
            if exchange_id
            else self._find_by_client_id(sym, client_order_id)
        )
        return self._order_state(raw)

    def cancel_order(self, symbol: str, exchange_id: str) -> bool:
        if not exchange_id:
            return False  # нечего отменять по пустому id — это НЕ успех (иначе снимали защиту «после отмены»)
        try:
            self._ex.cancel_order(exchange_id, canonical(symbol))
        except Exception as e:  # noqa: BLE001 - «уже нет» = цель достигнута
            if any(b.__name__ in ("OrderNotFound", "InvalidOrder") for b in type(e).__mro__):
                return True
            raise
        return True
