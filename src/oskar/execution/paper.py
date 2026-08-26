"""PaperAdapter — in-memory симулятор исполнения (фоллбек к VST).

Минимальная честная механика: market-вход исполняется по цене price_fn(symbol),
SL/TP хранятся как защитные уровни; mark(symbol, high, low) прогоняет бар и
закрывает позицию при касании (SL первым при коллизии — консервативно, как в
реплее). Инжектируемый price_fn — тесты управляют ценой.
"""

import itertools
from dataclasses import dataclass

from oskar.data.symbols import canonical
from oskar.execution.base import (
    BalanceInfo,
    ExchangeAdapter,
    PlacedOrder,
    PositionInfo,
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
        self._fills: list[PaperFill] = []
        self._ids = itertools.count(1)

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
        return PlacedOrder(str(next(self._ids)), client_order_id, entry_type.upper(), "closed")

    def place_sl(self, symbol, side, stop_price, client_order_id) -> PlacedOrder:
        self._positions[(canonical(symbol), side)].sl = stop_price
        return PlacedOrder(str(next(self._ids)), client_order_id, "STOP_MARKET", "open")

    def place_tp(self, symbol, side, stop_price, client_order_id) -> PlacedOrder:
        self._positions[(canonical(symbol), side)].tp = stop_price
        return PlacedOrder(str(next(self._ids)), client_order_id, "TAKE_PROFIT_MARKET", "open")

    def cancel_all(self, symbol: str) -> int:
        sym = canonical(symbol)
        n = 0
        for key in [k for k in self._positions if k[0] == sym]:
            pos = self._positions[key]
            n += int(pos.sl is not None) + int(pos.tp is not None)
            pos.sl = pos.tp = None
        return n

    def close_position(self, symbol: str, side: Side) -> PlacedOrder | None:
        sym = canonical(symbol)
        pos = self._positions.pop((sym, side), None)
        if pos is None:
            return None
        price = self._price(sym)
        self._settle(sym, pos, price, "manual_close")
        return PlacedOrder(str(next(self._ids)), "", "MARKET", "closed")

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
            elif tp_hit:
                settled.append(self._settle(sym, self._positions.pop(key), pos.tp, "tp_hit"))
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
