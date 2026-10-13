"""Монитор позиций — единственное место, где журнал догоняет биржу (ст. 1).

Раз в interval_seconds:
1. sweeper: pending_approvals с истекшим TTL -> expired (кнопки с карточки снимаются,
   decisions.outcome = approval_expired);
2. pending-сделки (лимитный вход): исполнилась -> open с ценой/qty/комиссией биржи;
   снята биржей, не найдена или старше limit_entry_ttl -> остаток снимается; если
   часть УЖЕ исполнена — позиция есть, ведем как open и защиту НЕ трогаем;
3. open-сделки: SL/TP исполнился -> closed с close_reason sl_hit/tp_hit и
   отменой парного ордера; защита исчезла при живой позиции -> переставляется
   (не удалось -> SAFE MODE: голую позицию не держим молча); позиции нет два
   тика подряд и защита не срабатывала -> закрыта извне (manual, exit/pnl NULL,
   reconciled=0); лосер старше max_loss_hold -> рыночное закрытие с ПОДТВЕРЖДЕНИЕМ
   по позициям -> duration_timeout (урок v1: лосеры жили в 3.4x дольше);
4. reconcile: позиции биржи vs open-сделки журнала (сумма qty по ключу, допуск 1%) —
   расхождение два тика подряд -> SAFE MODE + Telegram, один раз на ключ.

Все решения — по состоянию ордеров НА БИРЖЕ (order_status), не по ценам свечей.
Исключение по одной сделке не валит проход по остальным. realized_pnl — брутто
по ценам исполнения минус комиссии, если биржа их отдала (иначе брутто).
Один сомнительный опрос биржи (пустой positions()) защиту не снимает — только
подтвержденное отсутствие.
"""

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

from oskar.core.approvals import mark_decision
from oskar.execution.base import ExchangeAdapter, OrderState, PositionInfo
from oskar.journal.db import transaction

logger = logging.getLogger("oskar.monitor")

DEFAULT_INTERVAL_SEC = 60.0
QTY_TOLERANCE = 0.01  # 1%: округления биржи — не расхождение
CONFIRM_TICKS = 2  # отсутствие позиции / расхождение должны повториться


@dataclass
class MonitorReport:
    expired_cards: int = 0
    filled: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    closed: list[tuple[str, str]] = field(default_factory=list)  # (decision_id, reason)
    replaced: list[str] = field(default_factory=list)  # переставленная защита
    mismatches: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "expired_cards": self.expired_cards, "filled": len(self.filled),
            "cancelled": len(self.cancelled), "closed": len(self.closed),
            "replaced": len(self.replaced), "mismatches": len(self.mismatches),
            "errors": len(self.errors),
        }


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s[:-1] if s.endswith("Z") else s)
    except ValueError:
        return None
    # aware -> в UTC конвертацией, naive -> считаем UTC (все писатели пишут Z)
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


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
        state=None,  # RuntimeState: safe_mode при голой позиции / расхождении
        now_fn=lambda: datetime.now(UTC),
        interval_seconds: float = DEFAULT_INTERVAL_SEC,
    ):
        self._ex = adapter
        self._j = journal
        self._max_loss_hold = max_loss_hold_seconds
        self._limit_ttl = limit_entry_ttl_seconds
        self._notify = notify
        self._on_card_expired = on_card_expired
        self._state = state
        self._now = now_fn
        self._interval = interval_seconds
        self._reported_mismatches: set[str] = set()
        self._mismatch_streak: dict[str, int] = {}
        self._missing: dict[str, int] = {}  # decision_id -> тиков подряд без позиции
        self._replaced: dict[str, int] = {}  # "<decision>:<sl|tp>" -> сколько раз переставляли
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

    def _set_safe_mode(self, reason: str) -> None:
        if self._state is None or self._state.safe_mode:
            return
        self._state.safe_mode = True
        self._say(f"🛑 SAFE MODE: {reason} — новые входы заблокированы до ручного сброса")

    # --------------------------------------------------------------- sweeper

    def _sweep_expired(self, rep: MonitorReport) -> None:
        now = _iso(self._now())
        rows = self._j.execute(
            "SELECT decision_id, telegram_message_id FROM pending_approvals "
            "WHERE resolution IS NULL AND expires_at <= ?", (now,),
        ).fetchall()
        if not rows:
            return
        with transaction(self._j):
            for r in rows:
                self._j.execute(
                    "UPDATE pending_approvals SET resolution='expired', resolved_at=? "
                    "WHERE decision_id=? AND resolution IS NULL", (now, r["decision_id"]),
                )
                mark_decision(self._j, r["decision_id"], "approval_expired", "ttl")
        for r in rows:
            rep.expired_cards += 1
            if self._on_card_expired is not None:
                try:
                    self._on_card_expired(r["decision_id"], r["telegram_message_id"])
                except Exception as e:  # noqa: BLE001 - косметика карточки
                    logger.warning("card expire hook failed for %s: %s", r["decision_id"], e)
        logger.info("sweeper: %d pending approvals expired (fail-closed)", len(rows))

    # --------------------------------------------------------------- pending

    def _age_sec(self, t: sqlite3.Row) -> float:
        placed_at = _parse_iso(t["opened_at"]) or _parse_iso(t["created_at"]) or self._now()
        return (self._now() - placed_at).total_seconds()

    def _check_pending(self, t: sqlite3.Row, rep: MonitorReport) -> None:
        age = self._age_sec(t)
        try:
            st: OrderState | None = self._ex.order_status(
                t["symbol"], client_order_id=t["client_order_id"]
            )
        except Exception:
            if age < self._limit_ttl:
                raise  # транспорт/задержка биржи — повторим следующим тиком
            st = None  # за TTL ордера нет нигде: считаем снятым

        if st is not None and st.is_filled:
            self._promote(t, st, rep)
            return
        if st is not None and not st.is_gone and age < self._limit_ttl:
            return  # ждем цену

        why = "снят биржей" if st is None or st.is_gone else f"не исполнился за {int(age // 60)} мин"
        if st is not None and not st.is_gone:
            if not self._ex.cancel_order(t["symbol"], st.exchange_id):
                raise RuntimeError(f"entry {st.exchange_id!r} not cancelled - protection kept")
            try:  # между опросом и отменой часть могла исполниться
                st = self._ex.order_status(t["symbol"], exchange_id=st.exchange_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("post-cancel lookup failed for %s: %s", t["decision_id"], e)
        if st is not None and st.filled_qty > 0:
            self._promote(t, st, rep, partial=True)  # позиция ЕСТЬ — защиту не трогаем
            return

        self._cancel_protection(t)
        with transaction(self._j):
            self._j.execute(
                "UPDATE trades SET status='cancelled', closed_at=?, duration_seconds=? WHERE id=?",
                (_iso(self._now()), int(age), t["id"]),
            )
            self._j.execute(
                "UPDATE decisions SET outcome_detail=? WHERE decision_id=?",
                (f"limit_cancelled: {why}", t["decision_id"]),
            )
        rep.cancelled.append(t["decision_id"])
        self._say(f"⏳❌ {t['symbol']} {t['side']}: лимит {why}, SL/TP сняты")

    def _promote(self, t: sqlite3.Row, st: OrderState, rep: MonitorReport, partial: bool = False) -> None:
        price = st.avg_price or t["entry_price"]
        qty = st.filled_qty or t["qty"]
        with transaction(self._j):
            self._j.execute(
                "UPDATE trades SET status='open', entry_price=?, qty=?, opened_at=?, entry_fees=? "
                "WHERE id=?", (price, qty, _iso(self._now()), st.fee, t["id"]),
            )
        rep.filled.append(t["decision_id"])
        tag = " частично" if partial else ""
        self._say(f"✅ {t['symbol']} {t['side']}: лимит исполнен{tag} @ {price:.6g}, qty {qty:g}")

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

    def _close_if_protection_filled(self, t: sqlite3.Row, rep: MonitorReport) -> bool:
        sl = self._status_or_none(t["symbol"], t["sl_order_id"])
        if sl is not None and sl.is_filled:
            self._cancel_protection(t, keep=t["sl_order_id"])
            self._close(t, rep, sl.avg_price or t["sl_price"], "sl_hit", sl.fee)
            return True
        tp = self._status_or_none(t["symbol"], t["tp_order_id"])
        if tp is not None and tp.is_filled:
            self._cancel_protection(t, keep=t["tp_order_id"])
            self._close(t, rep, tp.avg_price or t["tp_price"], "tp_hit", tp.fee)
            return True
        self._last_protection = (sl, tp)
        return False

    def _check_open(self, t: sqlite3.Row, rep: MonitorReport) -> None:
        if self._close_if_protection_filled(t, rep):
            return
        sl, tp = self._last_protection
        did = t["decision_id"]

        pos = self._find_position(t["symbol"], t["side"])
        if pos is None:
            n = self._missing.get(did, 0) + 1
            self._missing[did] = n
            if n < CONFIRM_TICKS:
                logger.info("position %s not seen (%d/%d) - waiting a tick", did, n, CONFIRM_TICKS)
                return
            # подтвержденное отсутствие: защита могла сработать между опросами — перепроверка
            if self._close_if_protection_filled(t, rep):
                return
            self._missing.pop(did, None)
            self._cancel_protection(t)
            self._close(t, rep, None, "manual", None, reconciled=0)
            return
        self._missing.pop(did, None)

        # защита на месте? ордер отменен/исчез при живой позиции -> переставить
        replaced = False
        if sl is None or sl.is_gone:
            self._replace_protection(t, "sl", rep)
            replaced = True
        if tp is None or tp.is_gone:
            self._replace_protection(t, "tp", rep)
            replaced = True
        if replaced:
            return  # дальше — следующим тиком, с актуальными id

        opened = _parse_iso(t["opened_at"]) or self._now()
        age = (self._now() - opened).total_seconds()
        if pos.unrealized_pnl < 0 and age >= self._max_loss_hold:
            self._duration_stop(t, rep)

    def _replace_protection(self, t: sqlite3.Row, kind: str, rep: MonitorReport) -> None:
        key = f"{t['decision_id']}:{kind}"
        n = self._replaced.get(key, 0) + 1
        self._replaced[key] = n
        cid = f"{t['client_order_id']}-{kind}-r{n}"
        try:
            if kind == "sl":
                order = self._ex.place_sl(t["symbol"], t["side"], t["sl_price"], cid)
                column = "sl_order_id"
            else:
                order = self._ex.place_tp(t["symbol"], t["side"], t["tp_price"], cid)
                column = "tp_order_id"
            with transaction(self._j):
                self._j.execute(f"UPDATE trades SET {column}=? WHERE id=?", (order.exchange_id, t["id"]))
            rep.replaced.append(key)
            self._say(f"⚠️ {kind.upper()} переставлен: {t['symbol']} {t['side']} "
                      "(ордер исчез при живой позиции)")
        except Exception as e:  # noqa: BLE001 - голая позиция: громко и стоп новым входам
            rep.errors.append(f"{key}: replace failed: {e}")
            self._say(f"🛑 {t['symbol']} {t['side']} БЕЗ {kind.upper()}: переставить не удалось ({e})")
            self._set_safe_mode(f"позиция {t['symbol']} без защиты")

    def _duration_stop(self, t: sqlite3.Row, rep: MonitorReport) -> None:
        order = self._ex.close_position(t["symbol"], t["side"])
        if order is None:
            logger.info("duration-stop %s: position vanished before close; next tick", t["decision_id"])
            return
        still = self._find_position(t["symbol"], t["side"])
        if still is not None:
            self._say(f"⚠️ закрытие по времени {t['symbol']} {t['side']} не подтверждено — "
                      "позиция на бирже еще есть")
            self._set_safe_mode(f"неподтвержденное закрытие {t['symbol']}")
            return
        self._cancel_protection(t)
        exit_price, fee = None, None
        if order.exchange_id:
            try:
                st = self._ex.order_status(t["symbol"], exchange_id=order.exchange_id)
                exit_price, fee = st.avg_price, st.fee
            except Exception as e:  # noqa: BLE001 - цена выхода уточнится реконсиляцией
                logger.warning("close fill lookup failed for %s: %s", t["decision_id"], e)
        self._close(t, rep, exit_price, "duration_timeout", fee)

    def _close(
        self, t: sqlite3.Row, rep: MonitorReport,
        exit_price: float | None, reason: str, exit_fee: float | None, reconciled: int = 1,
    ) -> None:
        now = self._now()
        opened = _parse_iso(t["opened_at"])
        duration = int((now - opened).total_seconds()) if opened else None
        pnl = None
        if exit_price is not None and t["entry_price"] is not None and t["qty"]:
            sign = 1.0 if t["side"] == "long" else -1.0
            pnl = sign * (exit_price - t["entry_price"]) * t["qty"]
            pnl -= (t["entry_fees"] or 0.0) + (exit_fee or 0.0)
        with transaction(self._j):
            self._j.execute(
                "UPDATE trades SET status='closed', exit_price=?, close_reason=?, exit_fees=?, "
                "realized_pnl=?, closed_at=?, duration_seconds=?, reconciled=? WHERE id=?",
                (exit_price, reason, exit_fee, pnl, _iso(now), duration, reconciled, t["id"]),
            )
        rep.closed.append((t["decision_id"], reason))
        label = {"sl_hit": "🛑 стоп", "tp_hit": "🎯 тейк", "duration_timeout": "⏱ стоп по времени",
                 "manual": "✋ закрыта извне", "panic": "🚨 panic"}.get(reason, reason)
        pnl_txt = f", P&L {pnl:+.2f}" if pnl is not None else ""
        px_txt = f" @ {exit_price:.6g}" if exit_price is not None else ""
        self._say(f"{label}: {t['symbol']} {t['side']}{px_txt}{pnl_txt}")

    # ------------------------------------------------------------- reconcile

    def _reconcile(self, rep: MonitorReport) -> None:
        on_exchange: dict[tuple[str, str], float] = {}
        for p in self._ex.positions():
            if p.qty > 0:
                on_exchange[(p.symbol, p.side)] = on_exchange.get((p.symbol, p.side), 0.0) + p.qty
        in_journal: dict[tuple[str, str], float] = {}
        for r in self._trades("open"):
            key = (r["symbol"], r["side"])
            in_journal[key] = in_journal.get(key, 0.0) + float(r["qty"] or 0.0)
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

        # подтверждение вторым тиком: один сомнительный ответ биржи — не расхождение
        confirmed: list[str] = []
        for m in found:
            self._mismatch_streak[m] = self._mismatch_streak.get(m, 0) + 1
            if self._mismatch_streak[m] >= CONFIRM_TICKS:
                confirmed.append(m)
        for m in list(self._mismatch_streak):
            if m not in found:
                del self._mismatch_streak[m]
        rep.mismatches = confirmed
        for m in confirmed:
            if m not in self._reported_mismatches:
                self._reported_mismatches.add(m)
                self._say(f"⚠️ reconcile: {m}")
                self._set_safe_mode("журнал и биржа расходятся")
        self._reported_mismatches &= set(confirmed)  # ушло — можно сообщить снова

    # ----------------------------------------------------------------- panic

    def panic_flatten(self) -> dict:
        """Ст. 6: отменить все ордера и закрыть позиции. Флаги уже стоят — сюда после них.
        Ошибка по одной сделке не останавливает остальные."""
        rep = MonitorReport()
        out = {"cancelled": 0, "closed": 0, "errors": []}
        for t in self._trades("pending"):
            try:
                try:
                    st = self._ex.order_status(t["symbol"], client_order_id=t["client_order_id"])
                    if not st.is_gone and not st.is_filled:
                        self._ex.cancel_order(t["symbol"], st.exchange_id)
                except Exception as e:  # noqa: BLE001 - все равно снимаем защиту и закрываем запись
                    out["errors"].append(f"{t['decision_id']}: entry: {e}")
                self._cancel_protection(t)
                with transaction(self._j):
                    self._j.execute(
                        "UPDATE trades SET status='cancelled', closed_at=? WHERE id=?",
                        (_iso(self._now()), t["id"]),
                    )
                    self._j.execute(
                        "UPDATE decisions SET outcome_detail='panic' WHERE decision_id=?",
                        (t["decision_id"],),
                    )
                out["cancelled"] += 1
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"{t['decision_id']}: {e}")
        for t in self._trades("open"):
            try:
                order = self._ex.close_position(t["symbol"], t["side"])
                self._cancel_protection(t)
                exit_price, fee = None, None
                if order is not None and order.exchange_id:
                    try:
                        st = self._ex.order_status(t["symbol"], exchange_id=order.exchange_id)
                        exit_price, fee = st.avg_price, st.fee
                    except Exception as e:  # noqa: BLE001
                        logger.warning("panic close lookup failed for %s: %s", t["decision_id"], e)
                self._close(t, rep, exit_price, "panic", fee, reconciled=0)
                out["closed"] += 1
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"{t['decision_id']}: {e}")
        self._say(f"🚨 PANIC: снято входов {out['cancelled']}, закрыто позиций {out['closed']}, "
                  f"ошибок {len(out['errors'])}")
        return out

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
