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
    # время исполнения по бирже (ms): opened_at сделки — от него, а не от момента опроса
    # монитором (иначе SL, сработавший до первого опроса, выпадал из истории — Codex р.2 #6)
    timestamp_ms: int | None = None

    @property
    def is_filled(self) -> bool:
        return self.status == "filled"

    @property
    def is_gone(self) -> bool:
        """Ордера больше нет и он не исполнился."""
        return self.status in ("canceled", "expired", "rejected")


@dataclass(frozen=True)
class ProtectiveOrder:
    """Условный ордер защиты на бирже (SL/TP) — в том числе созданный САМОЙ
    биржей из stopLoss/takeProfit, прикрепленных ко входу (их id заранее
    неизвестны: монитор находит и «усыновляет» их после исполнения входа)."""

    exchange_id: str
    kind: str  # sl | tp
    trigger_price: float | None
    status: str  # open | filled | canceled | expired | rejected | unknown
    avg_price: float | None = None
    fee: float | None = None
    timestamp_ms: int | None = None  # время исполнения/обновления (для истории)
    client_order_id: str = ""
    qty: float | None = None  # объем ордера (у прикрепленных = объем входа)
    close_position: bool = False  # closePosition: закрывает всю позицию, объем не задан


@dataclass(frozen=True)
class OpenOrderRef:
    """Открытый ордер на бирже (любой символ) — для поиска «сирот» OSKAR."""

    symbol: str  # канонический
    exchange_id: str
    client_order_id: str
    kind: str  # entry | sl | tp | other


class OrderNotFoundError(RuntimeError):
    """Биржа ответила, что такого ордера нет. Отличается от сетевого сбоя:
    «нет ордера» — факт для решений монитора, «не дозвонились» — повод повторить."""


def cid_base(client_order_id: str) -> str:
    """База clientOrderId OSKAR: OSKAR-<run>-<hash> (без -sl/-tp/-rN), нижний регистр."""
    return "-".join(client_order_id.lower().split("-")[:3])


def owns_protection(o: ProtectiveOrder, *, owner_cid: str, position_qty: float | None) -> bool:
    """Принадлежит ли ордер защиты сделке. Ревью Codex раунд 2 (high #3): ручной стоп
    на 0.1 «усыновлялся» для позиции 2 — после его исполнения журнал закрывал всю сделку
    и снимал свой TP, а на бирже оставалось 1.9. Теперь:
    - чужой cid OSKAR (другая сделка) — не наш;
    - объем: closePosition или qty >= 99% позиции; частичный стоп всю позицию не защищает."""
    cid = o.client_order_id.lower()
    if cid.startswith("oskar-") and cid_base(cid) != cid_base(owner_cid):
        return False
    if o.close_position or position_qty is None:
        return True
    return o.qty is not None and o.qty >= position_qty * 0.99


def pick_protection(orders: list[ProtectiveOrder], kind: str, target: float,
                    tolerance_pct: float = 0.5, *, owner_cid: str | None = None,
                    position_qty: float | None = None) -> ProtectiveOrder | None:
    """Ордер защиты нужного вида с триггером ближе всего к целевой цене плана.
    Допуск отсекает чужие ордера на том же символе (ручные ордера Ивана); с owner_cid —
    еще и проверка принадлежности и объема (owns_protection)."""
    best, best_diff = None, None
    for o in orders:
        if o.kind != kind or o.trigger_price is None:
            continue
        if owner_cid is not None and not owns_protection(o, owner_cid=owner_cid, position_qty=position_qty):
            continue
        diff = abs(o.trigger_price - target)
        if diff > abs(target) * tolerance_pct / 100.0:
            continue
        if best_diff is None or diff < best_diff:
            best, best_diff = o, diff
    return best


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
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> PlacedOrder:
        """Вход. stop_loss/take_profit — защита, ПРИКРЕПЛЕННАЯ к ордеру: биржа
        создает SL/TP сама в момент исполнения (у лимитки до исполнения позиции
        нет, и отдельные closePosition-ордера BingX отвергает: 109420)."""

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

    @abstractmethod
    def protection_orders(
        self, symbol: str, side: Side, *, include_closed: bool = False
    ) -> list[ProtectiveOrder]:
        """SL/TP-ордера позиции symbol/side на бирже: открытые, а с include_closed —
        и история (исполненные/снятые) для классификации закрытия."""

    @abstractmethod
    def open_orders_all(self) -> list[OpenOrderRef]:
        """Все открытые ордера аккаунта по всем символам (поиск сирот OSKAR)."""
