"""ExchangeAdapter — архитектурный шов исполнения (решение сессии 2026-08-25:
DEX и MT5 адаптеры добавляются в будущем без переписывания ядра).

Внутри OSKAR стороны всегда long|short и канонические символы; маппинг на
биржевые условности (positionSide LONG/SHORT, buy/sell) — забота адаптера.
Каждый ордер обязан несет client_order_id (ст. 1: точная реконсиляция).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

Side = Literal["long", "short"]


@dataclass(frozen=True)
class PlacedOrder:
    exchange_id: str
    client_order_id: str
    type: str      # MARKET | LIMIT | STOP_MARKET | TAKE_PROFIT_MARKET
    status: str


@dataclass(frozen=True)
class PositionInfo:
    symbol: str    # канонический
    side: Side
    qty: float
    entry_price: float
    unrealized_pnl: float


@dataclass(frozen=True)
class BalanceInfo:
    total: float
    free: float
    used: float


@dataclass(frozen=True)
class OrderState:
    """Состояние ордера ПО ДАННЫМ БИРЖИ (ст. 1): монитор позиций решает по нему,
    а не по ценам свечей. status: open | filled | canceled | expired | rejected | unknown."""

    exchange_id: str
    status: str
    filled_qty: float = 0.0
    avg_price: float | None = None
    fee: float | None = None  # комиссия в quote, если биржа отдала

    @property
    def is_filled(self) -> bool:
        return self.status == "filled"

    @property
    def is_gone(self) -> bool:
        """Ордера больше нет и он не исполнился."""
        return self.status in ("canceled", "expired", "rejected")


class ExchangeAdapter(ABC):
    """Контракт исполнения. Реализации: BingXAdapter (VST/live), PaperAdapter,
    в будущем DexAdapter/Mt5Adapter."""

    @abstractmethod
    def normalize_qty(self, symbol: str, qty: float) -> float:
        """Количество к точности биржи. ОБЯЗАТЕЛЕН перед place_entry (qty-баг v1)."""

    @abstractmethod
    def set_leverage(self, symbol: str, leverage: float, side: Side) -> None: ...

    @abstractmethod
    def place_entry(
        self,
        symbol: str,
        side: Side,
        qty: float,
        client_order_id: str,
        *,
        entry_type: Literal["market", "limit"] = "market",
        price: float | None = None,
    ) -> PlacedOrder: ...

    @abstractmethod
    def place_sl(
        self, symbol: str, side: Side, stop_price: float, client_order_id: str
    ) -> PlacedOrder:
        """Отдельный STOP_MARKET, закрывающий позицию (BingX: inline SL нет)."""

    @abstractmethod
    def place_tp(
        self, symbol: str, side: Side, stop_price: float, client_order_id: str
    ) -> PlacedOrder: ...

    @abstractmethod
    def cancel_all(self, symbol: str) -> int:
        """Отмена всех открытых ордеров символа. Возвращает число отмененных."""

    @abstractmethod
    def close_position(self, symbol: str, side: Side) -> PlacedOrder | None:
        """Рыночное закрытие позиции (duration-stop, panic). None = позиции нет."""

    @abstractmethod
    def positions(self) -> list[PositionInfo]: ...

    @abstractmethod
    def balance(self) -> BalanceInfo: ...

    @abstractmethod
    def order_status(
        self, symbol: str, exchange_id: str = "", client_order_id: str = ""
    ) -> OrderState:
        """Состояние ордера по биржевому id или clientOrderId (нужен хотя бы один)."""

    @abstractmethod
    def cancel_order(self, symbol: str, exchange_id: str) -> bool:
        """Отмена одного ордера. True = отменен или его уже нет (идемпотентно)."""
