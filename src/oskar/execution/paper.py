"""PaperAdapter — in-memory симулятор исполнения (фоллбек к VST).

Минимальная честная механика: market-вход исполняется по цене price_fn(symbol),
SL/TP хранятся как защитные уровни; mark(symbol, high, low) прогоняет бар и
закрывает позицию при касании (SL первым при коллизии — консервативно, как в
реплее). Инжектируемый price_fn — тесты управляют ценой.

Ордера регистрируются по id с тем же контрактом order_status/cancel_order,
что у биржи: монитор позиций работает с paper так же, как с BingX.
"""

import itertools
from dataclasses import dataclass

from oskar.data.symbols import canonical
from oskar.execution.base import (
    BalanceInfo,
    ExchangeAdapter,
    OpenOrderRef,
    OrderNotFoundError,
    OrderState,
    PlacedOrder,
    PositionInfo,
    ProtectiveOrder,
    Side,
)


@dataclass
class _PaperPosition:
    side: Side
    qty: float
    entry_price: float
    sl: float | None = None
    tp: float | None = None
    client_order_id: str = ""
    sl_order_id: str = ""
    tp_order_id: str = ""


@dataclass
class _PaperOrder:
    symbol: str
    side: Side
    kind: str  # entry | sl | tp | close
    status: str  # open | filled | canceled
    client_order_id: str = ""
    avg_price: float | None = None
    qty: float = 0.0


@dataclass
class PaperFill:
    symbol: str
    side: Side
    qty: float
    price: float
    reason: str  # entry | sl_hit | tp_hit | manual_close


class PaperAdapter(ExchangeAdapter):
    def __init__(self, price_fn, *, start_balance: float = 1000.0, qty_precision: int = 6):
        self._price = price_fn
        self._balance = start_balance
        self._precision = qty_precision
        self._positions: dict[tuple[str, Side], _PaperPosition] = {}
        self._orders: dict[str, _PaperOrder] = {}
        self._fills: list[PaperFill] = []
        self._ids = itertools.count(1)

    def _register(
        self, symbol: str, side: Side, kind: str, status: str,
        client_order_id: str = "", price: float | None = None, qty: float = 0.0,
    ) -> str:
        oid = str(next(self._ids))
        self._orders[oid] = _PaperOrder(symbol, side, kind, status, client_order_id, price, qty)
        return oid

    def _set_status(self, oid: str, status: str, price: float | None = None) -> None:
        o = self._orders.get(oid)
        if o is not None and o.status == "open":
            o.status = status
            if price is not None:
                o.avg_price = price

    # ------------------------------------------------------------ contract

    def normalize_qty(self, symbol: str, qty: float) -> float:
        return round(qty, self._precision)

    def set_leverage(self, symbol: str, leverage: float, side: Side) -> None:
        pass  # paper: маржа не моделируется, риск считает лестница

    def place_entry(
        self,
        symbol: str,
        side: Side,
        qty: float,
        client_order_id: str,
        *,
        entry_type: str = "market",
        price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> PlacedOrder:
        sym = canonical(symbol)
        key = (sym, side)
        if key in self._positions:
            raise RuntimeError(f"paper: duplicate position {sym} {side}")
        fill_price = self._price(sym) if entry_type == "market" else float(price)
        self._positions[key] = _PaperPosition(
            side=side, qty=qty, entry_price=fill_price, client_order_id=client_order_id
        )
        self._fills.append(PaperFill(sym, side, qty, fill_price, "entry"))
        oid = self._register(sym, side, "entry", "filled", client_order_id, fill_price, qty)
        # прикрепленная защита: как у BingX — SL/TP появляются вместе с исполнением
        # (внутренняя регистрация: вход и его защита — одно действие биржи)
        if stop_loss is not None:
            self._protect(sym, side, "sl", stop_loss, f"{oid}-attached-sl")
        if take_profit is not None:
            self._protect(sym, side, "tp", take_profit, f"{oid}-attached-tp")
        return PlacedOrder(oid, client_order_id, entry_type.upper(), "closed")

    def _protect(self, sym: str, side: Side, kind: str, stop_price: float, client_order_id: str) -> str:
        oid = self._register(sym, side, kind, "open", client_order_id, stop_price)
        pos = self._positions.get((sym, side))
        if pos is not None:  # лимитка еще не исполнилась — условный ордер просто висит
            if kind == "sl":
                pos.sl, pos.sl_order_id = stop_price, oid
            else:
                pos.tp, pos.tp_order_id = stop_price, oid
        return oid

    def place_sl(self, symbol, side, stop_price, client_order_id) -> PlacedOrder:
        oid = self._protect(canonical(symbol), side, "sl", stop_price, client_order_id)
        return PlacedOrder(oid, client_order_id, "STOP_MARKET", "open")

    def place_tp(self, symbol, side, stop_price, client_order_id) -> PlacedOrder:
        oid = self._protect(canonical(symbol), side, "tp", stop_price, client_order_id)
        return PlacedOrder(oid, client_order_id, "TAKE_PROFIT_MARKET", "open")

    def cancel_all(self, symbol: str) -> int:
        sym = canonical(symbol)
        n = 0
        for key in [k for k in self._positions if k[0] == sym]:
            pos = self._positions[key]
            n += int(pos.sl is not None) + int(pos.tp is not None)
            self._set_status(pos.sl_order_id, "canceled")
            self._set_status(pos.tp_order_id, "canceled")
            pos.sl = pos.tp = None
        return n

    def cancel_order(self, symbol: str, exchange_id: str) -> bool:
        o = self._orders.get(exchange_id)
        if o is None or o.status != "open":
            return True  # нет или уже не открыт — цель достигнута
        o.status = "canceled"
        pos = self._positions.get((o.symbol, o.side))
        if pos is not None:
            if o.kind == "sl" and pos.sl_order_id == exchange_id:
                pos.sl = None
            if o.kind == "tp" and pos.tp_order_id == exchange_id:
                pos.tp = None
        return True

    def order_status(
        self, symbol: str, exchange_id: str = "", client_order_id: str = ""
    ) -> OrderState:
        o = self._orders.get(exchange_id) if exchange_id else None
        if o is None and client_order_id:
            matches = [(k, v) for k, v in self._orders.items() if v.client_order_id == client_order_id]
            if matches:
                exchange_id, o = matches[-1]
        if o is None:
            raise OrderNotFoundError(f"paper: unknown order {exchange_id or client_order_id}")
        filled = o.status == "filled"
        return OrderState(
            exchange_id=exchange_id, status=o.status,
            filled_qty=o.qty if filled else 0.0,
            avg_price=o.avg_price if filled else None,
        )

    def open_orders_all(self) -> list[OpenOrderRef]:
        return [
            OpenOrderRef(symbol=o.symbol, exchange_id=oid, client_order_id=o.client_order_id,
                         kind=o.kind if o.kind in ("entry", "sl", "tp") else "other")
            for oid, o in self._orders.items() if o.status == "open"
        ]

    def protection_orders(
        self, symbol: str, side: Side, *, include_closed: bool = False
    ) -> list[ProtectiveOrder]:
        sym = canonical(symbol)
        out = []
        for oid, o in self._orders.items():
            if o.symbol != sym or o.side != side or o.kind not in ("sl", "tp"):
                continue
            if o.status != "open" and not include_closed:
                continue
            # у paper-ордеров защиты avg_price хранит триггер (и он же цена исполнения)
            out.append(ProtectiveOrder(
                exchange_id=oid, kind=o.kind, trigger_price=o.avg_price, status=o.status,
                avg_price=o.avg_price if o.status == "filled" else None,
                client_order_id=o.client_order_id,
            ))
        return out

    def close_position(self, symbol: str, side: Side) -> PlacedOrder | None:
        sym = canonical(symbol)
        pos = self._positions.pop((sym, side), None)
        if pos is None:
            return None
        price = self._price(sym)
        self._settle(sym, pos, price, "manual_close")
        self._set_status(pos.sl_order_id, "canceled")
        self._set_status(pos.tp_order_id, "canceled")
        oid = self._register(sym, side, "close", "filled", "", price, pos.qty)
        return PlacedOrder(oid, "", "MARKET", "closed")

    def positions(self) -> list[PositionInfo]:
        out = []
        for (sym, _side), pos in self._positions.items():
            mark = self._price(sym)
            sign = 1.0 if pos.side == "long" else -1.0
            out.append(
                PositionInfo(
                    symbol=sym, side=pos.side, qty=pos.qty, entry_price=pos.entry_price,
                    unrealized_pnl=sign * (mark - pos.entry_price) * pos.qty,
                )
            )
        return out

    def balance(self) -> BalanceInfo:
        return BalanceInfo(total=self._balance, free=self._balance, used=0.0)

    # --------------------------------------------------------------- paper

    def mark(self, symbol: str, high: float, low: float) -> list[PaperFill]:
        """Прогон бара: SL/TP по high/low. SL первым при коллизии."""
        sym = canonical(symbol)
        settled: list[PaperFill] = []
        for key in [k for k in self._positions if k[0] == sym]:
            pos = self._positions[key]
            if pos.side == "long":
                sl_hit = pos.sl is not None and low <= pos.sl
                tp_hit = pos.tp is not None and high >= pos.tp
            else:
                sl_hit = pos.sl is not None and high >= pos.sl
                tp_hit = pos.tp is not None and low <= pos.tp
            if sl_hit:
                settled.append(self._settle(sym, self._positions.pop(key), pos.sl, "sl_hit"))
                self._set_status(pos.sl_order_id, "filled", pos.sl)
                self._set_status(pos.tp_order_id, "canceled")
            elif tp_hit:
                settled.append(self._settle(sym, self._positions.pop(key), pos.tp, "tp_hit"))
                self._set_status(pos.tp_order_id, "filled", pos.tp)
                self._set_status(pos.sl_order_id, "canceled")
        return settled

    def _settle(self, sym: str, pos: _PaperPosition, price: float, reason: str) -> PaperFill:
        sign = 1.0 if pos.side == "long" else -1.0
        self._balance += sign * (price - pos.entry_price) * pos.qty
        fill = PaperFill(sym, pos.side, pos.qty, price, reason)
        self._fills.append(fill)
        return fill

    @property
    def fills(self) -> list[PaperFill]:
        return list(self._fills)
