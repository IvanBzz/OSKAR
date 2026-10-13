"""Исполнение по кнопке: план -> ордера на бирже -> запись в журнал.

Одно нажатие делает всё (пожелание Ивана): нормализация qty под точность
биржи, установка левериджа, вход (market или limit по найденному уровню),
и ОТДЕЛЬНЫМИ ордерами SL/TP (BingX hedge не принимает inline).

Расчетов здесь нет: ExecutionPlan посчитан и проверен лестницей до показа
карточки (ст. 2). Кнопка выбирает только способ входа. Но между карточкой и
нажатием проходит до часа — поэтому перед входом короткая проверка «мир не
изменился»: лимит позиций и дубль по журналу, цена не ушла за стоп/цель и не
уплыла от плана дальше max_drift_pct.

Инварианты (ревью 2026-10-13, класс бага v1 с двойной позицией):
- вход ушел на биржу => запись в trades есть ВСЕГДА, даже если защита не
  встала (журнал не врет, ст. 1); исход несет entry_sent=True — откатывать
  approve по такому исходу запрещено;
- защита не встала => позицию не держим: рыночный вход закрывается, лимитный
  снимается; если и это не удалось — громкий алерт, статус open/pending без
  SL/TP, монитор переставит защиту или закроет;
- повторное нажатие по исполненному решению — no-op (UNIQUE decision_id).
"""

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from oskar.contracts.schemas import ExecutionPlan
from oskar.data.client import exchange_error_code
from oskar.execution.base import ExchangeAdapter, PlacedOrder
from oskar.journal.db import transaction

logger = logging.getLogger("oskar.executor")

DEFAULT_MAX_DRIFT_PCT = 0.5  # цена ушла дальше — план устарел, маркет не ставим


class ExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionOutcome:
    ok: bool
    message: str
    client_order_id: str = ""
    entry_type: str = ""
    entry_sent: bool = False  # вход ушел на биржу: откат approve ЗАПРЕЩЕН


def make_client_order_id(run_id: str, decision_id: str) -> str:
    """Короткий и уникальный: хэш вместо обрезки (обрезка `[:36]` теряла
    направление у длинных символов -> коллизия long/short, ст. 1)."""
    digest = hashlib.sha1(decision_id.encode("utf-8")).hexdigest()[:12]
    return f"OSKAR-{run_id}-{digest}"


def short_error(e: Exception) -> str:
    """Код биржи + начало текста: без подписанных URL и простыней в Telegram."""
    code = exchange_error_code(e)
    text = str(e).replace("\n", " ")[:120]
    return f"[{code}] {text}" if code else f"{type(e).__name__}: {text}"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ExecutionService:
    def __init__(
        self,
        adapter: ExchangeAdapter,
        journal: sqlite3.Connection,
        run_id: str,
        *,
        price_fn=None,  # (symbol) -> float: последняя цена; None = проверка цены выключена
        max_drift_pct: float = DEFAULT_MAX_DRIFT_PCT,
        max_open_positions: int | None = None,
    ):
        self._ex = adapter
        self._j = journal
        self._run_id = run_id
        self._price_fn = price_fn
        self._max_drift_pct = max_drift_pct
        self._max_open = max_open_positions

    def plan_for(self, decision_id: str) -> ExecutionPlan:
        row = self._j.execute(
            "SELECT card_json FROM pending_approvals WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise ExecutionError(f"нет плана для {decision_id}")
        return ExecutionPlan.model_validate_json(row["card_json"])

    def already_executed(self, decision_id: str) -> bool:
        row = self._j.execute(
            "SELECT 1 FROM trades WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        return row is not None

    # ----------------------------------------------------------- pre-checks

    def _live_trades(self, exclude_decision: str) -> list[sqlite3.Row]:
        return self._j.execute(
            "SELECT symbol, side FROM trades WHERE status IN ('open','pending') AND decision_id != ?",
            (exclude_decision,),
        ).fetchall()

    def _pre_checks(self, plan: ExecutionPlan, mode: str) -> tuple[str | None, float | None]:
        """Мир на момент нажатия. Лестница видела его при создании карточки.
        Возвращает (причина отказа | None, текущая цена | None)."""
        live = self._live_trades(plan.candidate_id)
        if self._max_open is not None and len(live) >= self._max_open:
            return f"лимит открытых позиций: {len(live)}/{self._max_open}", None
        if any(r["symbol"] == plan.symbol and r["side"] == plan.side for r in live):
            return f"уже есть позиция {plan.symbol} {plan.side}", None
        if self._price_fn is None:
            return None, None
        try:
            px = float(self._price_fn(plan.symbol))
        except Exception as e:  # noqa: BLE001 - без цены вслепую не входим
            return f"нет текущей цены: {short_error(e)}", None
        is_long = plan.side == "long"
        if (is_long and px <= plan.sl) or (not is_long and px >= plan.sl):
            return f"цена {px:.6g} уже за стопом {plan.sl:.6g}", px
        if (is_long and px >= plan.tp) or (not is_long and px <= plan.tp):
            return f"цена {px:.6g} уже за целью {plan.tp:.6g}", px
        if mode == "market":
            drift = abs(px - plan.entry_market) / plan.entry_market * 100.0
            if drift > self._max_drift_pct:
                return (f"цена ушла на {drift:.2f}% от плана "
                        f"({plan.entry_market:.6g} -> {px:.6g}) — план устарел"), px
        return None, px

    @staticmethod
    def risk_sized_qty(plan: ExecutionPlan, entry_price: float) -> float:
        """qty под ФАКТИЧЕСКУЮ цену входа: план хранит одну qty для обоих входов, а
        риск = qty * |entry - SL|. Кнопка «Маркет» при цене дальше от стопа, чем
        проверенный лестницей вход, раздувала риск (Codex 2026-10-13: $10 -> $30).
        Детерминированная арифметика, не решение модели (ст. 2). Только вниз."""
        dist = abs(entry_price - plan.sl)
        if dist <= 0:
            return plan.qty
        return min(plan.qty, plan.risk_usd / dist)

    # -------------------------------------------------------------- execute

    def execute(self, decision_id: str, mode: str) -> ExecutionOutcome:
        """mode: market | limit. Возвращает исход; исключения не пробрасывает."""
        if mode not in ("market", "limit"):
            return ExecutionOutcome(False, f"неизвестный режим {mode}")
        if self.already_executed(decision_id):
            return ExecutionOutcome(False, "уже исполнено")

        try:
            plan = self.plan_for(decision_id)
        except Exception as e:  # noqa: BLE001
            return ExecutionOutcome(False, f"план не прочитан: {e}")

        if mode == "limit" and plan.entry_limit is None:
            return ExecutionOutcome(False, "лимитного уровня нет в плане — только маркет")
        reason, px = self._pre_checks(plan, mode)
        if reason is not None:
            logger.info("execution refused for %s: %s", decision_id, reason)
            return ExecutionOutcome(False, reason)

        client_order_id = make_client_order_id(self._run_id, decision_id)
        price = plan.entry_market if mode == "market" else float(plan.entry_limit)
        # риск считается от цены, по которой реально войдем: маркет — текущая, лимит — уровень
        sizing_price = px if (mode == "market" and px is not None) else price
        target_qty = self.risk_sized_qty(plan, sizing_price)

        # 1. вход: до этой точки на бирже ничего нет — любой отказ честно без следов
        try:
            qty = self._ex.normalize_qty(plan.symbol, target_qty)
            if qty <= 0:
                return ExecutionOutcome(False, f"qty после нормализации = {qty}")
            self._ex.set_leverage(plan.symbol, plan.leverage, plan.side)
            entry_order = self._ex.place_entry(
                plan.symbol, plan.side, qty, client_order_id,
                entry_type=mode, price=None if mode == "market" else price,
            )
        except Exception as e:  # noqa: BLE001 - биржа отказала до входа
            logger.error("entry failed for %s: %s", decision_id, e)
            return ExecutionOutcome(False, f"биржа отказала: {short_error(e)}")

        # 2. защита: с этого места запись в журнал — ОБЯЗАТЕЛЬНА при любом исходе
        sl_order = tp_order = None
        protection_error: Exception | None = None
        try:
            sl_order = self._ex.place_sl(plan.symbol, plan.side, plan.sl, client_order_id + "-sl")
            tp_order = self._ex.place_tp(plan.symbol, plan.side, plan.tp, client_order_id + "-tp")
        except Exception as e:  # noqa: BLE001
            protection_error = e
            logger.error("protection failed for %s after entry: %s", decision_id, e)

        flattened = False
        if protection_error is not None:
            flattened = self._flatten(plan, entry_order, sl_order, mode)

        status, entry_price, entry_fee = self._entry_state(plan, entry_order, mode, price)
        close_reason = closed_at = None
        if protection_error is not None and flattened:
            status = "closed" if mode == "market" else "cancelled"
            close_reason = "manual" if mode == "market" else None
            closed_at = _now_iso()

        detail = f"{mode}@{price:.8g}"
        if protection_error is not None:
            detail += f"; protection_failed: {short_error(protection_error)}; flattened={flattened}"
        with transaction(self._j):
            self._j.execute(
                "INSERT OR IGNORE INTO trades (run_id, decision_id, client_order_id, symbol, "
                "side, status, entry_price, qty, leverage, risk_usd, sl_price, tp_price, "
                "sl_order_id, tp_order_id, setup_type, timeframe, reasoning, entry_fees, "
                "close_reason, closed_at, opened_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self._run_id, decision_id, client_order_id, plan.symbol, plan.side,
                    status, entry_price, qty, plan.leverage, plan.risk_usd, plan.sl, plan.tp,
                    sl_order.exchange_id if sl_order else None,
                    tp_order.exchange_id if tp_order else None,
                    "", plan.timeframe, plan.tldr, entry_fee, close_reason, closed_at, _now_iso(),
                ),
            )
            self._j.execute(
                "UPDATE decisions SET outcome='executed', outcome_detail=? WHERE decision_id=?",
                (detail, decision_id),
            )

        if protection_error is not None:
            tail = ("позиция закрыта, вход снят" if flattened
                    else "ЗАКРЫТЬ НЕ УДАЛОСЬ — проверь биржу СРОЧНО")
            msg = (f"⚠️ вход исполнен, но защита не встала ({short_error(protection_error)}); "
                   f"{tail}")
            logger.critical("%s %s", decision_id, msg)
            return ExecutionOutcome(False, msg, client_order_id, mode, entry_sent=True)

        label = "маркет" if mode == "market" else f"лимит {price:.6g}"
        logger.info("executed %s %s qty=%s lev=%s (%s) status=%s",
                    decision_id, label, qty, plan.leverage, entry_order.exchange_id, status)
        return ExecutionOutcome(
            True, f"{label}: {plan.symbol} {plan.side} qty {qty:g}, плечо {plan.leverage:g}x",
            client_order_id=client_order_id, entry_type=mode, entry_sent=True,
        )

    # -------------------------------------------------------------- helpers

    def _entry_state(
        self, plan: ExecutionPlan, entry_order: PlacedOrder, mode: str, price: float
    ) -> tuple[str, float, float | None]:
        """Статус/цена/комиссия входа ПО БИРЖЕ: маркет мог не исполниться, а
        цена исполнения отличается от плановой (слиппедж) — журнал не выдумывает."""
        if mode == "limit":
            return "pending", price, None
        try:
            st = self._ex.order_status(plan.symbol, exchange_id=entry_order.exchange_id)
        except Exception as e:  # noqa: BLE001 - уточнит монитор/реконсиляция
            logger.warning("entry fill lookup failed for %s: %s", entry_order.exchange_id, e)
            return "open", price, None
        if st.is_filled:
            return "open", st.avg_price or price, st.fee
        return "pending", price, None

    def _flatten(self, plan: ExecutionPlan, entry_order: PlacedOrder, sl_order, mode: str) -> bool:
        """Защита не встала — позицию без стопа не держим."""
        try:
            if mode == "market":
                self._ex.close_position(plan.symbol, plan.side)
            else:
                self._ex.cancel_order(plan.symbol, entry_order.exchange_id)
            if sl_order is not None:
                self._ex.cancel_order(plan.symbol, sl_order.exchange_id)
            return True
        except Exception as e:  # noqa: BLE001
            logger.critical("flatten failed for %s %s: %s", plan.symbol, plan.side, e)
            return False


def equity_guarded(
    execute_fn,
    equity_source_fn,
    *,
    equity_fn=None,  # () -> float: живой equity
    plan_fn=None,  # (decision_id) -> ExecutionPlan
    risk_cap_pct: float | None = None,  # caps.risk_per_trade_pct
    tolerance: float = 0.10,
):
    """Обертка исполнения: без свежего equity с биржи ордера не ставим (ст. 1).

    Протухший ключ, бан, сеть — любой сбой приватного API делает сайзинг
    выдуманным; план посчитан на equity, которого может не быть. Кнопка
    честно отвечает причиной, карточка остается живой.

    Вторая проверка: план мог быть посчитан на ДРУГОМ equity (дефолтные $1000
    до первого ответа биржи) — риск плана сверяется с живым equity и капсом.
    """

    def guarded(decision_id: str, mode: str) -> ExecutionOutcome:
        source = equity_source_fn()
        if source != "exchange":
            return ExecutionOutcome(
                False,
                f"equity с биржи недоступен (источник: {source}) — исполнение "
                "заблокировано. Проверь API-ключ BingX.",
            )
        if equity_fn is not None and plan_fn is not None and risk_cap_pct is not None:
            try:
                plan = plan_fn(decision_id)
                equity = float(equity_fn())
            except Exception as e:  # noqa: BLE001
                return ExecutionOutcome(False, f"план/equity не прочитаны: {e}")
            allowed = risk_cap_pct / 100.0 * equity * (1.0 + tolerance)
            if plan.risk_usd > allowed:
                return ExecutionOutcome(
                    False,
                    f"план посчитан на другой equity: риск ${plan.risk_usd:.2f} > "
                    f"${allowed:.2f} допустимых при equity ${equity:.2f} — пропусти карточку",
                )
        return execute_fn(decision_id, mode)

    return guarded
