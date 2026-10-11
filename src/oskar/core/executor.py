"""Исполнение по кнопке: план -> ордера на бирже -> запись в журнал.

Одно нажатие делает всё (пожелание Ивана): нормализация qty под точность
биржи, установка левериджа, вход (market или limit по найденному уровню),
и ОТДЕЛЬНЫМИ ордерами SL/TP (BingX hedge не принимает inline).

Расчетов здесь нет: ExecutionPlan посчитан и проверен лестницей до показа
карточки (ст. 2). Кнопка выбирает только способ входа.

Идемпотентность: повторное нажатие по уже исполненному решению — no-op
(UNIQUE decision_id в trades + явная проверка), чтобы дубль клика не
породил вторую позицию (класс бага v1).
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from oskar.contracts.schemas import ExecutionPlan
from oskar.execution.base import ExchangeAdapter

logger = logging.getLogger("oskar.executor")


class ExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionOutcome:
    ok: bool
    message: str
    client_order_id: str = ""
    entry_type: str = ""


class ExecutionService:
    def __init__(self, adapter: ExchangeAdapter, journal: sqlite3.Connection, run_id: str):
        self._ex = adapter
        self._j = journal
        self._run_id = run_id

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

        client_order_id = f"OSKAR-{self._run_id}-{decision_id}"[:36]
        price = plan.entry_market if mode == "market" else float(plan.entry_limit)

        try:
            qty = self._ex.normalize_qty(plan.symbol, plan.qty)
            if qty <= 0:
                return ExecutionOutcome(False, f"qty после нормализации = {qty}")
            self._ex.set_leverage(plan.symbol, plan.leverage, plan.side)
            entry_order = self._ex.place_entry(
                plan.symbol, plan.side, qty, client_order_id,
                entry_type=mode, price=None if mode == "market" else price,
            )
            sl_order = self._ex.place_sl(plan.symbol, plan.side, plan.sl, client_order_id + "-sl")
            tp_order = self._ex.place_tp(plan.symbol, plan.side, plan.tp, client_order_id + "-tp")
        except Exception as e:  # noqa: BLE001 - биржа: честно сообщаем и не пишем сделку
            logger.error("execution failed for %s: %s", decision_id, e)
            return ExecutionOutcome(False, f"биржа отказала: {e}")

        # status: market исполняется сразу, limit ждет цену
        status = "open" if mode == "market" else "pending"
        self._j.execute(
            "INSERT OR IGNORE INTO trades (run_id, decision_id, client_order_id, symbol, "
            "side, status, entry_price, qty, leverage, risk_usd, sl_price, tp_price, "
            "sl_order_id, tp_order_id, setup_type, timeframe, reasoning, opened_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._run_id, decision_id, client_order_id, plan.symbol, plan.side,
                status, price, qty, plan.leverage, plan.risk_usd, plan.sl, plan.tp,
                sl_order.exchange_id, tp_order.exchange_id, "", plan.timeframe,
                plan.tldr, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            ),
        )
        self._j.execute(
            "UPDATE decisions SET outcome='executed', outcome_detail=? WHERE decision_id=?",
            (f"{mode}@{price:.8g}", decision_id),
        )
        self._j.commit()

        label = "маркет" if mode == "market" else f"лимит {price:.6g}"
        logger.info("executed %s %s qty=%s lev=%s (%s)",
                    decision_id, label, qty, plan.leverage, entry_order.exchange_id)
        return ExecutionOutcome(
            True, f"{label}: {plan.symbol} {plan.side} qty {qty:g}, плечо {plan.leverage:g}x",
            client_order_id=client_order_id, entry_type=mode,
        )


def equity_guarded(execute_fn, equity_source_fn):
    """Обертка исполнения: без свежего equity с биржи ордера не ставим (ст. 1).

    Протухший ключ, бан, сеть — любой сбой приватного API делает сайзинг
    выдуманным; план посчитан на equity, которого может не быть. Кнопка
    честно отвечает причиной, карточка остается живой.
    """

    def guarded(decision_id: str, mode: str) -> ExecutionOutcome:
        source = equity_source_fn()
        if source != "exchange":
            return ExecutionOutcome(
                False,
                f"equity с биржи недоступен (источник: {source}) — исполнение "
                "заблокировано. Проверь API-ключ BingX.",
            )
        return execute_fn(decision_id, mode)

    return guarded
