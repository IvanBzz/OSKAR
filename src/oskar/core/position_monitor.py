"""Монитор позиций — единственное место, где журнал догоняет биржу (ст. 1).

Раз в interval_seconds:
1. sweeper: pending_approvals с истекшим TTL -> expired (кнопки с карточки снимаются);
2. pending-сделки (лимитный вход): исполнилась -> open; отменена биржей или
   старше limit_entry_ttl -> снять вход и SL/TP, status cancelled;
3. open-сделки: SL/TP исполнился -> closed с close_reason sl_hit/tp_hit и
   отменой парного ордера; позиции на бирже нет, а защита не исполнялась ->
   закрыта извне (manual); лосер старше max_loss_hold -> duration_timeout
   (рыночное закрытие + снятие защиты; урок v1: лосеры жили в 3.4x дольше);
4. reconcile: позиции биржи vs open-сделки журнала — расхождение в лог и в
   Telegram, один раз на ключ.

Все решения — по состоянию ордеров НА БИРЖЕ (order_status), не по ценам свечей.
Исключение по одной сделке не валит проход по остальным. realized_pnl — брутто
по ценам исполнения минус комиссии, если биржа их отдала (иначе брутто).
"""

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

from oskar.execution.base import ExchangeAdapter, OrderState, PositionInfo

logger = logging.getLogger("oskar.monitor")

DEFAULT_INTERVAL_SEC = 60.0
QTY_TOLERANCE = 0.01  # 1%: округления биржи — не расхождение


@dataclass
class MonitorReport:
    expired_cards: int = 0
    filled: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    closed: list[tuple[str, str]] = field(default_factory=list)  # (decision_id, reason)
    mismatches: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "expired_cards": self.expired_cards, "filled": len(self.filled),
            "cancelled": len(self.cancelled), "closed": len(self.closed),
            "mismatches": len(self.mismatches), "errors": len(self.errors),
        }


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:-1] if s.endswith("Z") else s).replace(tzinfo=UTC)
    except ValueError:
        return None


class PositionMonitor:
    def __init__(
        self,
        adapter: ExchangeAdapter,
        journal: sqlite3.Connection,
        *,
        max_loss_hold_seconds: int,
        limit_entry_ttl_seconds: int,
        notify=None,  # (text) -> None: короткие уведомления в Telegram
        on_card_expired=None,  # (decision_id, telegram_message_id) -> None
        now_fn=lambda: datetime.now(UTC),
        interval_seconds: float = DEFAULT_INTERVAL_SEC,
    ):
        self._ex = adapter
        self._j = journal
        self._max_loss_hold = max_loss_hold_seconds
        self._limit_ttl = limit_entry_ttl_seconds
        self._notify = notify
        self._on_card_expired = on_card_expired
        self._now = now_fn
        self._interval = interval_seconds
        self._reported_mismatches: set[str] = set()
        self.last_report: dict = {}
        self.ticks = 0
        self._stop = threading.Event()

    # ------------------------------------------------------------------ tick

    def tick(self) -> MonitorReport:
        rep = MonitorReport()
        self._guard(rep, "sweeper", lambda: self._sweep_expired(rep))
        for t in self._trades("pending"):
            self._guard(rep, t["decision_id"], lambda t=t: self._check_pending(t, rep))
        for t in self._trades("open"):
            self._guard(rep, t["decision_id"], lambda t=t: self._check_open(t, rep))
        self._guard(rep, "reconcile", lambda: self._reconcile(rep))
        self.ticks += 1
        self.last_report = rep.as_dict()
        return rep

    def _guard(self, rep: MonitorReport, label: str, fn) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - одна сделка не валит проход
            rep.errors.append(f"{label}: {type(e).__name__}: {e}")
            logger.warning("monitor %s: %s: %s", label, type(e).__name__, e)

    def _trades(self, status: str) -> list[sqlite3.Row]:
        return self._j.execute(
            "SELECT * FROM trades WHERE status = ? ORDER BY id", (status,)
        ).fetchall()

    def _say(self, text: str) -> None:
        logger.info(text)
        if self._notify is not None:
            try:
                self._notify(text)
            except Exception as e:  # noqa: BLE001 - канал не влияет на учет
                logger.warning("notify failed: %s", e)

    # --------------------------------------------------------------- sweeper

    def _sweep_expired(self, rep: MonitorReport) -> None:
        now = _iso(self._now())
        rows = self._j.execute(
            "SELECT decision_id, telegram_message_id FROM pending_approvals "
            "WHERE resolution IS NULL AND expires_at <= ?", (now,),
        ).fetchall()
        for r in rows:
            self._j.execute(
                "UPDATE pending_approvals SET resolution='expired', resolved_at=? "
                "WHERE decision_id=? AND resolution IS NULL", (now, r["decision_id"]),
            )
            rep.expired_cards += 1
            if self._on_card_expired is not None:
                try:
                    self._on_card_expired(r["decision_id"], r["telegram_message_id"])
                except Exception as e:  # noqa: BLE001 - косметика карточки
                    logger.warning("card expire hook failed for %s: %s", r["decision_id"], e)
        if rows:
            self._j.commit()
            logger.info("sweeper: %d pending approvals expired (fail-closed)", len(rows))

    # --------------------------------------------------------------- pending

    def _check_pending(self, t: sqlite3.Row, rep: MonitorReport) -> None:
        st = self._ex.order_status(t["symbol"], client_order_id=t["client_order_id"])
        now = self._now()
        if st.is_filled:
            price = st.avg_price or t["entry_price"]
            self._j.execute(
                "UPDATE trades SET status='open', entry_price=?, opened_at=?, entry_fees=? "
                "WHERE id=?", (price, _iso(now), st.fee, t["id"]),
            )
            self._j.commit()
            rep.filled.append(t["decision_id"])
            self._say(f"✅ {t['symbol']} {t['side']}: лимит исполнен @ {price:.6g}")
            return

        placed_at = _parse_iso(t["opened_at"]) or _parse_iso(t["created_at"]) or now
        age = (now - placed_at).total_seconds()
        if not st.is_gone and age < self._limit_ttl:
            return  # ждем цену

        why = "снят биржей" if st.is_gone else f"не исполнился за {int(age // 60)} мин"
        if not st.is_gone:
            self._ex.cancel_order(t["symbol"], st.exchange_id)
        self._cancel_protection(t)
        self._j.execute(
            "UPDATE trades SET status='cancelled', closed_at=?, duration_seconds=? WHERE id=?",
            (_iso(now), int(age), t["id"]),
        )
        self._j.execute(
            "UPDATE decisions SET outcome_detail=? WHERE decision_id=?",
            (f"limit_cancelled: {why}", t["decision_id"]),
        )
        self._j.commit()
        rep.cancelled.append(t["decision_id"])
        self._say(f"⏳❌ {t['symbol']} {t['side']}: лимит {why}, SL/TP сняты")

    # ------------------------------------------------------------------ open

    def _status_or_none(self, symbol: str, order_id: str | None) -> OrderState | None:
        if not order_id:
            return None
        return self._ex.order_status(symbol, exchange_id=order_id)

    def _find_position(self, symbol: str, side: str) -> PositionInfo | None:
        for p in self._ex.positions():
            if p.symbol == symbol and p.side == side and p.qty > 0:
                return p
        return None

    def _cancel_protection(self, t: sqlite3.Row, keep: str | None = None) -> None:
        for oid in (t["sl_order_id"], t["tp_order_id"]):
            if oid and oid != keep:
                try:
                    self._ex.cancel_order(t["symbol"], oid)
                except Exception as e:  # noqa: BLE001 - учет важнее косметики
                    logger.warning("cancel %s for %s failed: %s", oid, t["decision_id"], e)

    def _check_open(self, t: sqlite3.Row, rep: MonitorReport) -> None:
        sl = self._status_or_none(t["symbol"], t["sl_order_id"])
        if sl is not None and sl.is_filled:
            self._cancel_protection(t, keep=t["sl_order_id"])
            self._close(t, rep, sl.avg_price or t["sl_price"], "sl_hit", sl.fee)
            return
        tp = self._status_or_none(t["symbol"], t["tp_order_id"])
        if tp is not None and tp.is_filled:
            self._cancel_protection(t, keep=t["tp_order_id"])
            self._close(t, rep, tp.avg_price or t["tp_price"], "tp_hit", tp.fee)
            return

        pos = self._find_position(t["symbol"], t["side"])
        if pos is None:
            # защита не исполнялась, а позиции нет: закрыли руками/извне
            self._cancel_protection(t)
            self._close(t, rep, None, "manual", None)
            return

        opened = _parse_iso(t["opened_at"]) or self._now()
        age = (self._now() - opened).total_seconds()
        if pos.unrealized_pnl < 0 and age >= self._max_loss_hold:
            order = self._ex.close_position(t["symbol"], t["side"])
            self._cancel_protection(t)
            exit_price, fee = None, None
            if order is not None and order.exchange_id:
                try:
                    st = self._ex.order_status(t["symbol"], exchange_id=order.exchange_id)
                    exit_price, fee = st.avg_price, st.fee
                except Exception as e:  # noqa: BLE001 - цена выхода уточнится реконсиляцией
                    logger.warning("close fill lookup failed for %s: %s", t["decision_id"], e)
            self._close(t, rep, exit_price, "duration_timeout", fee)

    def _close(
        self, t: sqlite3.Row, rep: MonitorReport,
        exit_price: float | None, reason: str, exit_fee: float | None,
    ) -> None:
        now = self._now()
        opened = _parse_iso(t["opened_at"])
        duration = int((now - opened).total_seconds()) if opened else None
        pnl = None
        if exit_price is not None and t["entry_price"] is not None and t["qty"]:
            sign = 1.0 if t["side"] == "long" else -1.0
            pnl = sign * (exit_price - t["entry_price"]) * t["qty"]
            pnl -= (t["entry_fees"] or 0.0) + (exit_fee or 0.0)
        self._j.execute(
            "UPDATE trades SET status='closed', exit_price=?, close_reason=?, exit_fees=?, "
            "realized_pnl=?, closed_at=?, duration_seconds=?, reconciled=1 WHERE id=?",
            (exit_price, reason, exit_fee, pnl, _iso(now), duration, t["id"]),
        )
        self._j.commit()
        rep.closed.append((t["decision_id"], reason))
        label = {"sl_hit": "🛑 стоп", "tp_hit": "🎯 тейк", "duration_timeout": "⏱ стоп по времени",
                 "manual": "✋ закрыта извне"}.get(reason, reason)
        pnl_txt = f", P&L {pnl:+.2f}" if pnl is not None else ""
        px_txt = f" @ {exit_price:.6g}" if exit_price is not None else ""
        self._say(f"{label}: {t['symbol']} {t['side']}{px_txt}{pnl_txt}")

    # ------------------------------------------------------------- reconcile

    def _reconcile(self, rep: MonitorReport) -> None:
        on_exchange = {(p.symbol, p.side): p.qty for p in self._ex.positions() if p.qty > 0}
        in_journal = {
            (r["symbol"], r["side"]): float(r["qty"] or 0.0) for r in self._trades("open")
        }
        found: list[str] = []
        for key, qty in on_exchange.items():
            if key not in in_journal:
                found.append(f"позиция на бирже без сделки в журнале: {key[0]} {key[1]} qty {qty:g}")
            elif in_journal[key] and abs(qty - in_journal[key]) / in_journal[key] > QTY_TOLERANCE:
                found.append(
                    f"qty расходится: {key[0]} {key[1]} биржа {qty:g} / журнал {in_journal[key]:g}"
                )
        for key in in_journal.keys() - on_exchange.keys():
            found.append(f"сделка в журнале без позиции на бирже: {key[0]} {key[1]}")
        rep.mismatches = found
        for m in found:
            if m not in self._reported_mismatches:
                self._reported_mismatches.add(m)
                self._say(f"⚠️ reconcile: {m}")
        self._reported_mismatches &= set(found)  # ушло с биржи/из журнала — можно сообщить снова

    # ---------------------------------------------------------------- thread

    def run_forever(self) -> None:
        logger.info("position monitor started (interval=%.0fs)", self._interval)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - следующий тик попробует снова
                logger.error("monitor tick failed: %s", e)
            self._stop.wait(self._interval)

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="oskar-monitor", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
