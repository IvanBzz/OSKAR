"""Монитор позиций — единственное место, где журнал догоняет биржу (ст. 1).

Два ритма:
- быстрый (fast_interval, 5 с) — только pending-входы: лимитка исполнилась ->
  open + защита в тот же проход (окно «позиция без стопа» — секунды, а у входа
  с прикрепленными SL/TP его нет вовсе);
- полный (interval, 60 с) — всё ниже.

1. sweeper: pending_approvals с истекшим TTL -> expired (кнопки с карточки снимаются,
   decisions.outcome = approval_expired);
2. pending-сделки: исполнилась -> open с ценой/qty/комиссией биржи и защитой;
   снята биржей, не найдена или старше TTL -> остаток снимается; если часть УЖЕ
   исполнена — позиция есть, ведем как open и защиту НЕ трогаем. TTL: лимитка —
   limit_entry_ttl профиля, вход с неизвестным исходом (таймаут) — 3 мин;
3. open-сделки: SL/TP исполнился -> closed (sl_hit/tp_hit) с отменой парного;
   защиты нет/исчезла при живой позиции -> сначала «усыновить» с биржи (SL/TP,
   прикрепленные ко входу, биржа создает сама — их id заранее неизвестны), иначе
   поставить; не встает — повтор (109420 сразу после исполнения: позиция еще не
   видна), затем SAFE MODE (голую позицию молча не держим). Позиции нет два тика
   подряд -> по истории биржи: сработал SL/TP -> sl_hit/tp_hit, иначе manual
   (exit/pnl NULL); лосер старше max_loss_hold -> рыночное закрытие с
   ПОДТВЕРЖДЕНИЕМ по позициям -> duration_timeout (урок v1: лосеры жили в 3.4x дольше);
4. reconcile: позиции биржи vs open-сделки журнала (сумма qty по ключу, допуск 1%) —
   расхождение два тика подряд -> SAFE MODE + Telegram, один раз на ключ;
5. стоп по просадке (блок H, 2026-10-16): пик equity и защелка drawdown_halt в meta
   (risk/drawdown.py). Защелка закрывает только НОВЫЕ входы — сопровождение и защита
   открытых позиций выше идут как обычно.

SAFE MODE пишется в meta (переживает рестарт, core/emergency.py). Алерты — с
экранированием под parse_mode=HTML и ошибками биржи через short_error (без подписанных
URL): «&» или «<» в тексте не должны молча глушить алерт «БЕЗ SL».

Все решения — по состоянию ордеров НА БИРЖЕ, не по ценам свечей. Исключение по
одной сделке не валит проход по остальным. realized_pnl — брутто по ценам
исполнения минус комиссии, если биржа их отдала (иначе брутто).
"""

import html
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from oskar.core.approvals import mark_decision
from oskar.core.emergency import engage_safe_mode
from oskar.core.executor import short_error
from oskar.data.client import exchange_error_code
from oskar.execution.base import (
    ExchangeAdapter,
    OrderNotFoundError,
    OrderState,
    PositionInfo,
    cid_base,
    owns_protection,
    pick_protection,
)
from oskar.journal.db import transaction

logger = logging.getLogger("oskar.monitor")

DEFAULT_INTERVAL_SEC = 60.0
DEFAULT_FAST_INTERVAL_SEC = 5.0
QTY_TOLERANCE = 0.01  # 1%: округления биржи — не расхождение
CONFIRM_TICKS = 2  # отсутствие позиции / расхождение должны повториться
UNKNOWN_ENTRY_TTL_SEC = 180  # вход после таймаута не найден за 3 мин — его нет
PROTECT_RETRIES = 3  # 109420 сразу после исполнения: позиция еще не видна бирже
POSITION_MISSING_CODE = "109420"
ORPHAN_SCAN_SEC = 600  # поиск ордеров OSKAR без сделки в журнале — при старте и раз в 10 мин


@dataclass
class MonitorReport:
    expired_cards: int = 0
    filled: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    closed: list[tuple[str, str]] = field(default_factory=list)  # (decision_id, reason)
    replaced: list[str] = field(default_factory=list)  # поставленная монитором защита
    adopted: list[str] = field(default_factory=list)  # защита, найденная на бирже
    orphans: list[str] = field(default_factory=list)  # снятые ордера OSKAR без сделки
    mismatches: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "expired_cards": self.expired_cards, "filled": len(self.filled),
            "cancelled": len(self.cancelled), "closed": len(self.closed),
            "replaced": len(self.replaced), "adopted": len(self.adopted), "orphans": len(self.orphans),
            "mismatches": len(self.mismatches), "errors": len(self.errors),
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
        fast_interval_seconds: float = DEFAULT_FAST_INTERVAL_SEC,
        reconciler=None,  # ClosedTradeReconciler: tick(now) — сверка закрытых по филлам биржи
        drawdown=None,  # DrawdownGuard: пик equity и защелка просадки на полном тике
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
        self._fast_interval = fast_interval_seconds
        self._reconciler = reconciler
        self._drawdown = drawdown
        self._reported_mismatches: set[str] = set()
        self._mismatch_streak: dict[str, int] = {}
        self._missing: dict[str, int] = {}  # decision_id -> тиков подряд без позиции
        self._replaced: dict[str, int] = {}  # "<decision>:<sl|tp>" -> сколько раз ставили
        self._protect_fail: dict[str, int] = {}  # "<decision>:<sl|tp>" -> неудач подряд
        self._orphan_scan_at: datetime | None = None
        self.last_report: dict = {}
        self.ticks = 0
        self._stop = threading.Event()

    # ------------------------------------------------------------------ tick

    def tick(self) -> MonitorReport:
        rep = MonitorReport()
        self._guard(rep, "sweeper", lambda: self._sweep_expired(rep))
        self._pending_pass(rep)
        for t in self._trades("open"):
            self._guard(rep, t["decision_id"], lambda t=t: self._check_open(t, rep))
        self._guard(rep, "reconcile", lambda: self._reconcile(rep))
        self._guard(rep, "orphans", lambda: self._scan_orphans(rep))
        if self._reconciler is not None:
            self._guard(rep, "closed_reconcile", lambda: self._reconciler.tick(self._now()))
        if self._drawdown is not None:
            self._guard(rep, "drawdown", self._check_drawdown)
        self.ticks += 1
        self.last_report = rep.as_dict()
        return rep

    def tick_pending(self) -> MonitorReport:
        """Быстрый проход: pending-входы (исполнение лимитки -> защита) и open-сделки без
        подтвержденного SL (109420 сразу после исполнения — повтор через 5 с, а не через
        минуту полного тика; идея Codex раунд 2)."""
        rep = MonitorReport()
        self._pending_pass(rep)
        for t in self._j.execute(
            "SELECT * FROM trades WHERE status='open' AND sl_order_id IS NULL ORDER BY id"
        ).fetchall():
            if self._inflight(t):
                continue
            self._guard(rep, t["decision_id"], lambda t=t: self._protect_if_visible(t, rep))
        return rep

    def _protect_if_visible(self, t: sqlite3.Row, rep: MonitorReport) -> None:
        if self._find_position(t["symbol"], t["side"]) is not None:
            self._ensure_protection(t, rep)

    def _pending_pass(self, rep: MonitorReport) -> None:
        for t in self._trades("pending"):
            self._guard(rep, t["decision_id"], lambda t=t: self._check_pending(t, rep))

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

    def _reload(self, t: sqlite3.Row) -> sqlite3.Row:
        return self._j.execute("SELECT * FROM trades WHERE id = ?", (t["id"],)).fetchone()

    def _say(self, text: str) -> None:
        logger.info(text)
        if self._notify is not None:
            try:  # notify = send_text с parse_mode=HTML: «P&L», «<» из ошибки биржи — экранируем
                self._notify(html.escape(text, quote=False))
            except Exception as e:  # noqa: BLE001 - канал не влияет на учет
                logger.warning("notify failed: %s", e)

    def _set_safe_mode(self, reason: str) -> None:
        if self._state is None or self._state.safe_mode:
            return
        engage_safe_mode(self._state, reason)  # флаг + meta: переживает рестарт (блок H)
        self._say(f"🛑 SAFE MODE: {reason} — новые входы заблокированы до ручного сброса")

    def _check_drawdown(self) -> None:
        halt = self._drawdown.check()
        if halt is not None:
            self._say(f"🛑 {halt.message()}. Открытые позиции сопровождаются; снять — "
                      "oskar drawdown-reset")

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

    def _entry_state(self, t: sqlite3.Row) -> OrderState:
        """Вход по биржевому id (сохранен исполнителем); без id — по clientOrderId
        (вход после таймаута / старые записи), найденный id запоминаем."""
        if t["entry_order_id"]:
            return self._ex.order_status(t["symbol"], exchange_id=t["entry_order_id"])
        st = self._ex.order_status(t["symbol"], client_order_id=t["client_order_id"])
        if st.exchange_id:
            with transaction(self._j):
                self._j.execute("UPDATE trades SET entry_order_id=? WHERE id=?", (st.exchange_id, t["id"]))
        return st

    def _inflight(self, t: sqlite3.Row) -> bool:
        from oskar.core.executor import inflight_client_ids

        return cid_base(str(t["client_order_id"])) in inflight_client_ids()

    def _check_pending(self, t: sqlite3.Row, rep: MonitorReport) -> None:
        if self._inflight(t):
            return  # исполнитель еще ставит ордера этой сделки — не мешаем
        age = self._age_sec(t)
        ttl = UNKNOWN_ENTRY_TTL_SEC if t["entry_type"] == "market" else self._limit_ttl
        try:
            st: OrderState | None = self._entry_state(t)
        except OrderNotFoundError:
            if age < ttl:
                return  # биржа еще не показала ордер (таймаут на входе) — ждем
            st = None  # за TTL ордера нет нигде: считаем снятым
        # сетевые ошибки идут наверх: _guard, повтор следующим тиком (не «снят»)

        if st is not None and st.is_filled:
            self._promote(t, st, rep)
            self._ensure_protection(self._reload(t), rep)
            return
        partial = st is not None and not st.is_gone and st.filled_qty > 0
        if st is not None and not st.is_gone and age < ttl and not partial:
            return  # ждем цену
        # Частичный филл НЕ ждет TTL: часть позиции уже есть, а у голой лимитки защиты нет
        # (Codex раунд 2, high #4). Остаток снимается, исполненное — open + защита сейчас.

        why = ("снят биржей" if st is None or st.is_gone
               else "исполнен частично — остаток снят" if partial
               else f"не исполнился за {int(age // 60)} мин")
        if st is not None and not st.is_gone:
            if not self._ex.cancel_order(t["symbol"], st.exchange_id):
                raise RuntimeError(f"entry {st.exchange_id!r} not cancelled - protection kept")
            try:  # между опросом и отменой часть могла исполниться
                st = self._ex.order_status(t["symbol"], exchange_id=st.exchange_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("post-cancel lookup failed for %s: %s", t["decision_id"], e)
        if st is not None and st.filled_qty > 0:
            self._promote(t, st, rep, partial=True)  # позиция ЕСТЬ — защиту не трогаем
            self._ensure_protection(self._reload(t), rep)
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
        kind = "вход" if t["entry_type"] == "market" else "лимит"
        self._say(f"⏳❌ {t['symbol']} {t['side']}: {kind} {why}")

    def _promote(self, t: sqlite3.Row, st: OrderState, rep: MonitorReport, partial: bool = False) -> None:
        price = st.avg_price or t["entry_price"]
        qty = st.filled_qty or t["qty"]
        # opened_at — момент исполнения ПО БИРЖЕ, а не момент нашего опроса: SL, сработавший
        # до первого опроса, иначе выпадал из истории (Codex раунд 2, medium #6)
        filled_at = (datetime.fromtimestamp(st.timestamp_ms / 1000, tz=UTC)
                     if st.timestamp_ms else self._now())
        with transaction(self._j):
            self._j.execute(
                "UPDATE trades SET status='open', entry_price=?, qty=?, opened_at=?, entry_fees=?, "
                "entry_order_id=COALESCE(entry_order_id, ?) WHERE id=?",
                (price, qty, _iso(filled_at), st.fee, st.exchange_id or None, t["id"]),
            )
        rep.filled.append(t["decision_id"])
        tag = " частично" if partial else ""
        what = "вход" if t["entry_type"] == "market" else "лимит"
        self._say(f"✅ {t['symbol']} {t['side']}: {what} исполнен{tag} @ {price:.6g}, qty {qty:g}")

    # ------------------------------------------------------------ protection

    def _protection_state(self, symbol: str, order_id: str | None) -> OrderState | None:
        if not order_id:
            return None
        try:
            return self._ex.order_status(symbol, exchange_id=order_id)
        except OrderNotFoundError:
            return OrderState(exchange_id=order_id, status="canceled")  # нет на бирже = нет защиты

    def _set_order_id(self, t: sqlite3.Row, kind: str, order_id: str) -> None:
        column = "sl_order_id" if kind == "sl" else "tp_order_id"
        with transaction(self._j):
            self._j.execute(f"UPDATE trades SET {column}=? WHERE id=?", (order_id, t["id"]))

    def _ensure_protection(self, t: sqlite3.Row, rep: MonitorReport,
                           sl: OrderState | None = None, tp: OrderState | None = None) -> bool:
        """SL/TP на месте? Нет id или ордер исчез -> найти на бирже (прикрепленные ко
        входу), иначе поставить. True — что-то поменяли в этом проходе."""
        sl = sl if sl is not None else self._protection_state(t["symbol"], t["sl_order_id"])
        tp = tp if tp is not None else self._protection_state(t["symbol"], t["tp_order_id"])
        need = [kind for kind, st in (("sl", sl), ("tp", tp)) if st is None or st.is_gone]
        if not need:
            return False
        try:
            existing = [o for o in self._ex.protection_orders(t["symbol"], t["side"])
                        if o.status == "open"]
        except Exception as e:  # noqa: BLE001
            # не смогли проверить защиту — это НЕ «ее нет» и не повод молчать: ставим свою
            # (дубль closePosition-стопа безвреден, голая позиция — нет); не встанет —
            # счетчик неудач и SAFE MODE (Codex раунд 2, high #5)
            logger.warning("%s: protection list unavailable (%s) - placing own", t["decision_id"], e)
            existing = []
        for kind in need:
            target = t["sl_price"] if kind == "sl" else t["tp_price"]
            found = pick_protection(existing, kind, target, owner_cid=str(t["client_order_id"]),
                                    position_qty=float(t["qty"] or 0) or None)
            if found is not None:
                self._set_order_id(t, kind, found.exchange_id)
                self._protect_fail.pop(f"{t['decision_id']}:{kind}", None)
                rep.adopted.append(f"{t['decision_id']}:{kind}")
                logger.info("%s %s adopted from exchange: %s", t["decision_id"], kind, found.exchange_id)
                continue
            self._place_protection(t, kind, rep, had_id=bool(t[f"{kind}_order_id"]))
        return True

    def _place_protection(self, t: sqlite3.Row, kind: str, rep: MonitorReport, had_id: bool) -> None:
        key = f"{t['decision_id']}:{kind}"
        n = self._replaced.get(key, 0) + 1
        cid = f"{t['client_order_id']}-{kind}-r{n}"
        try:
            if kind == "sl":
                order = self._ex.place_sl(t["symbol"], t["side"], t["sl_price"], cid)
            else:
                order = self._ex.place_tp(t["symbol"], t["side"], t["tp_price"], cid)
        except Exception as e:  # noqa: BLE001
            fails = self._protect_fail.get(key, 0) + 1
            self._protect_fail[key] = fails
            rep.errors.append(f"{key}: place failed ({fails}): {e}")
            if exchange_error_code(e) == POSITION_MISSING_CODE and fails < PROTECT_RETRIES:
                logger.warning("%s: position not visible yet (%d/%d), retry next tick", key, fails,
                               PROTECT_RETRIES)
                return
            if kind == "tp":
                if fails == PROTECT_RETRIES or exchange_error_code(e) != POSITION_MISSING_CODE:
                    self._say(f"⚠️ {t['symbol']} {t['side']}: TP не встал ({short_error(e)}); "
                              "стоп на месте")
                return  # без TP позиция защищена стопом — safe mode не нужен
            self._say(f"🛑 {t['symbol']} {t['side']} БЕЗ SL: поставить не удалось ({short_error(e)})")
            self._set_safe_mode(f"позиция {t['symbol']} без защиты")
            return
        self._replaced[key] = n
        self._protect_fail.pop(key, None)
        self._set_order_id(t, kind, order.exchange_id)
        rep.replaced.append(key)
        if had_id:
            self._say(f"⚠️ {kind.upper()} переставлен: {t['symbol']} {t['side']} "
                      "(ордер исчез при живой позиции)")
        else:
            self._say(f"🛡 {kind.upper()} выставлен: {t['symbol']} {t['side']} "
                      f"@ {t['sl_price'] if kind == 'sl' else t['tp_price']:.6g}")

    def _cancel_protection(self, t: sqlite3.Row, keep: str | None = None) -> None:
        for oid in (t["sl_order_id"], t["tp_order_id"]):
            if oid and oid != keep:
                try:
                    self._ex.cancel_order(t["symbol"], oid)
                except Exception as e:  # noqa: BLE001 - учет важнее косметики
                    logger.warning("cancel %s for %s failed: %s", oid, t["decision_id"], e)

    # ------------------------------------------------------------------ open

    def _find_position(self, symbol: str, side: str) -> PositionInfo | None:
        for p in self._ex.positions():
            if p.symbol == symbol and p.side == side and p.qty > 0:
                return p
        return None

    def _close_if_protection_filled(self, t: sqlite3.Row, rep: MonitorReport) -> bool:
        sl = self._protection_state(t["symbol"], t["sl_order_id"])
        if sl is not None and sl.is_filled:
            self._cancel_protection(t, keep=t["sl_order_id"])
            self._close(t, rep, sl.avg_price or t["sl_price"], "sl_hit", sl.fee)
            return True
        tp = self._protection_state(t["symbol"], t["tp_order_id"])
        if tp is not None and tp.is_filled:
            self._cancel_protection(t, keep=t["tp_order_id"])
            self._close(t, rep, tp.avg_price or t["tp_price"], "tp_hit", tp.fee)
            return True
        self._last_protection = (sl, tp)
        return False

    def _close_by_exchange_history(self, t: sqlite3.Row, rep: MonitorReport) -> bool:
        """Позиции нет, а id защиты журналу не известны (прикрепленные SL/TP не успели
        усыновить): спросить историю биржи — какой из них исполнился после входа."""
        try:
            history = self._ex.protection_orders(t["symbol"], t["side"], include_closed=True)
        except Exception as e:  # noqa: BLE001 - закроем как manual, реконсиляция уточнит
            logger.warning("history lookup failed for %s: %s", t["decision_id"], e)
            return False
        # окно — от создания записи (вход отправлен), а не от opened_at: тот мог быть
        # временем нашего опроса, позже срабатывания стопа (Codex раунд 2, medium #6)
        since = _parse_iso(t["created_at"]) or _parse_iso(t["opened_at"])
        since_ms = int(since.timestamp() * 1000) - 60_000 if since else 0
        qty = float(t["qty"] or 0) or None
        def _target(o):
            return t["sl_price"] if o.kind == "sl" else t["tp_price"]

        mine = [o for o in history
                if o.exchange_id in (t["sl_order_id"], t["tp_order_id"])
                or owns_protection(o, owner_cid=str(t["client_order_id"]), position_qty=qty,
                                   target_price=_target(o))]
        fills = [o for o in mine if o.status == "filled"
                 and (o.timestamp_ms is None or o.timestamp_ms >= since_ms)]
        if not fills:
            return False
        last = max(fills, key=lambda o: o.timestamp_ms or 0)
        self._cancel_protection(t)
        for o in mine:  # парный ордер мог остаться открытым (только свои — ручные не трогаем)
            if o.status == "open":
                try:
                    self._ex.cancel_order(t["symbol"], o.exchange_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("cancel sibling %s failed: %s", o.exchange_id, e)
        price = last.avg_price or last.trigger_price
        self._close(t, rep, price, "sl_hit" if last.kind == "sl" else "tp_hit", last.fee)
        return True

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
            if self._close_if_protection_filled(t, rep) or self._close_by_exchange_history(t, rep):
                self._missing.pop(did, None)
                return
            self._missing.pop(did, None)
            self._cancel_protection(t)
            self._close(t, rep, None, "manual", None)
            return
        self._missing.pop(did, None)

        if self._ensure_protection(t, rep, sl, tp):
            return  # дальше — следующим тиком, с актуальными id

        opened = _parse_iso(t["opened_at"]) or self._now()
        age = (self._now() - opened).total_seconds()
        if pos.unrealized_pnl < 0 and age >= self._max_loss_hold:
            self._duration_stop(t, rep)

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
        exit_price: float | None, reason: str, exit_fee: float | None,
    ) -> None:
        """Закрытие по данным монитора. reconciled=0 ВСЕГДА: «сверено» ставит только
        sync/reconcile.py по филлам биржи (ст. 1 п. 1); иначе sl/tp/duration-закрытия
        сверка не увидела бы никогда, а INV-003 навсегда осталась бы без данных."""
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
                "realized_pnl=?, closed_at=?, duration_seconds=?, reconciled=0 WHERE id=?",
                (exit_price, reason, exit_fee, pnl, _iso(now), duration, t["id"]),
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

    # --------------------------------------------------------------- orphans

    def _scan_orphans(self, rep: MonitorReport) -> None:
        """Открытые ордера OSKAR (clientOrderId «OSKAR-...»), которых журнал не знает:
        процесс упал между отправкой входа и записью, или после закрытия сделки
        остался ее SL/TP. Сирота-вход позже открыл бы позицию мимо журнала, а
        забытый closePosition-стоп закрыл бы СЛЕДУЮЩУЮ позицию по символу — снимаем.
        Чужие ордера (ручные, прикрепленные биржей без нашего cid) не трогаем."""
        now = self._now()
        last = self._orphan_scan_at
        if last is not None and (now - last).total_seconds() < ORPHAN_SCAN_SEC:
            return
        self._orphan_scan_at = now
        from oskar.core.executor import inflight_client_ids

        # Порядок важен (перепроверка Codex): сначала список ордеров, потом реестр «в
        # процессе», потом журнал. Ордер из списка поставлен исполнением, которое
        # зарегистрировалось ДО постановки: оно либо еще в реестре, либо уже записало
        # сделку (запись — до снятия с реестра) и видно в журнале, читаемом последним.
        orders = self._ex.open_orders_all()
        known_cids = set(inflight_client_ids())
        live = self._j.execute(
            "SELECT client_order_id, entry_order_id, sl_order_id, tp_order_id FROM trades "
            "WHERE status IN ('pending','open')"
        ).fetchall()
        known_ids = {x for r in live for x in (r["entry_order_id"], r["sl_order_id"], r["tp_order_id"]) if x}
        known_cids |= {cid_base(str(r["client_order_id"])) for r in live}
        for o in orders:
            cid = o.client_order_id.lower()
            if not cid.startswith("oskar-"):
                continue
            if o.exchange_id in known_ids or cid_base(cid) in known_cids:
                continue
            self._ex.cancel_order(o.symbol, o.exchange_id)
            rep.orphans.append(f"{o.symbol}:{o.kind}:{o.exchange_id}")
            if o.kind == "entry":
                self._say(f"⚠️ снят вход-сирота {o.symbol} ({o.client_order_id}): "
                          "ордер OSKAR без сделки в журнале")
            else:
                logger.info("orphan %s %s %s cancelled (%s)", o.kind, o.symbol, o.exchange_id,
                            o.client_order_id)

    # ----------------------------------------------------------------- panic

    def panic_flatten(self) -> dict:
        """Ст. 6: отменить все ордера и закрыть позиции. Флаги уже стоят — сюда после них.
        Ошибка по одной сделке не останавливает остальные."""
        rep = MonitorReport()
        out = {"cancelled": 0, "closed": 0, "errors": []}
        for t in self._trades("pending"):
            try:
                try:
                    st = self._entry_state(t)
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
                for o in self._ex.protection_orders(t["symbol"], t["side"]):  # прикрепленные без id
                    if o.status == "open":
                        self._ex.cancel_order(t["symbol"], o.exchange_id)
                exit_price, fee = None, None
                if order is not None and order.exchange_id:
                    try:
                        st = self._ex.order_status(t["symbol"], exchange_id=order.exchange_id)
                        exit_price, fee = st.avg_price, st.fee
                    except Exception as e:  # noqa: BLE001
                        logger.warning("panic close lookup failed for %s: %s", t["decision_id"], e)
                self._close(t, rep, exit_price, "panic", fee)
                out["closed"] += 1
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"{t['decision_id']}: {e}")
        self._say(f"🚨 PANIC: снято входов {out['cancelled']}, закрыто позиций {out['closed']}, "
                  f"ошибок {len(out['errors'])}")
        return out

    # ---------------------------------------------------------------- thread

    def _has_pending(self) -> bool:
        # pending-входы ИЛИ open без подтвержденного SL (перепроверка Codex: при одной open
        # без стопа быстрый проход не запускался — повтор ждал полного тика)
        return self._j.execute(
            "SELECT 1 FROM trades WHERE status='pending' "
            "OR (status='open' AND sl_order_id IS NULL) LIMIT 1"
        ).fetchone() is not None

    def run_forever(self) -> None:
        logger.info("position monitor started (interval=%.0fs, pending=%.0fs)",
                    self._interval, self._fast_interval)
        last_full = float("-inf")
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                if now - last_full >= self._interval:
                    self.tick()
                    last_full = now
                elif self._has_pending():
                    self.tick_pending()
            except Exception as e:  # noqa: BLE001 - следующий тик попробует снова
                logger.error("monitor tick failed: %s", e)
            self._stop.wait(self._fast_interval)

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="oskar-monitor", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
