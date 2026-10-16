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
   открытых позиций выше идут как обычно;
6. сироты (раз в 10 мин и при старте демона ДО первого входа, блок I 2026-10-16): ордер
   OSKAR своего запуска без сделки — снять; чужого запуска (вторая машина?) — не трогать,
   SAFE MODE + алерт. Позиция без сделки при старте — так же (SAFE MODE, не закрываем).

SAFE MODE пишется в meta (переживает рестарт, core/emergency.py). Алерты — с
экранированием под parse_mode=HTML и ошибками биржи через short_error (без подписанных
URL): «&» или «<» в тексте не должны молча глушить алерт «БЕЗ SL».

Ревью Codex H2/I, 2026-10-16:
- старт сверяет ПОЗИЦИИ первыми, потом сирот: свой SL/TP-сирота снимается, только если на его
  паре symbol/side подтверждено отсутствие позиции без сделки (раньше старт снимал единственный
  SL потерянной сделки до сверки позиций); сохраненный — один алерт на ордер;
- чужие ордера/позиции: SAFE MODE взводится на КАЖДОМ скане, пока они есть (дедуп — только
  алерта): panic-reset при оставшихся ордерах второй машины снимал защиту навсегда; после
  reset скан — на ближайшем тике, а не через ORPHAN_SCAN_SEC;
- алерты panic копятся в буфере потока (deferred_notes) и уходят после биржевых действий,
  вне замка flatten.

Ревью Codex H3, 3-й круг (2026-10-16) — упрощение защиты. Принцип: монитор НЕ закрывает и НЕ
берет под учет то, что не доказано своим по идентификаторам; неоднозначность — SAFE MODE + алерт
+ решение оператора. Слежение chase (закрытие по symbol/side после panic) и автовосстановление
потерянной сделки на старте убраны: первое закрывало ручную позицию той же пары, не проверяло
биржу, пока исполнитель в процессе, и терялось при рестарте; второе присваивало ручную позицию.
- panic закрывает ровно qty своей сделки или входа, исполнение которого доказано по id ордера /
  clientOrderId (close_qty), и снимает его защиту по id; по паре «на всякий случай» — ничего;
- исход входа не доказан — запись в список неясных (meta, переживает рестарт), SAFE MODE, один
  алерт; тик проверяет такие входы по id (и пока исполнитель в процессе) и напоминает о них;
- при взведенном panic (в том числе восстановленном после рестарта) сделки не идут в обычное
  сопровождение: pending — исход по id, open — учет по id ордеров защиты и алерт; новая защита
  не ставится, стоп по времени не работает;
- позиция без сделки на старте — SAFE MODE и алерт с данными позиции и ордерами OSKAR на ней;
  учет не восстанавливается, SL/TP OSKAR на этой паре не снимаются.

Ревью H3, 4-й круг (2026-10-16):
- вход, закрытый по clientOrderId, не закрывается второй раз: повторный /panic берет сохраненную
  запись (closed/manual), запись — upsert, а исход, решенный процессом, — еще и в памяти
  (_cid_done), проверяемой перед любым close_qty: сбой записи meta не повторяет закрытие;
- panic до close_qty open-сделки проверяет ее SL/TP по id: сработал — учет sl_hit/tp_hit без
  закрытия (иначе срезал бы ручную часть пары); проверить не удалось — не закрывает, SAFE MODE;
- тик под panic держит _FLATTEN_LOCK только на том, что может закрыть по id, уступая его ждущему
  engage_panic между элементами; учет прочих open-сделок — вне замка. Закрытие в журнале — только
  из open/pending (panic и тик не перетирают учет друг друга).

Все решения — по состоянию ордеров НА БИРЖЕ, не по ценам свечей. Исключение по
одной сделке не валит проход по остальным. realized_pnl — брутто по ценам
исполнения минус комиссии, если биржа их отдала (иначе брутто).
"""

import contextlib
import html
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from oskar.contracts.schemas import ExecutionPlan
from oskar.core.approvals import mark_decision
from oskar.core.emergency import (
    add_unclear_entry,
    drop_unclear_entry,
    engage_safe_mode,
    hold_safe_mode,
    unclear_entries,
    update_unclear_entry,
    upsert_unclear_entry,
)
from oskar.core.executor import short_error
from oskar.core.run_registry import cid_run_id, run_known
from oskar.data.client import exchange_error_code
from oskar.execution.base import (
    ExchangeAdapter,
    OpenOrderRef,
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
UNCLEAR_REMIND_SEC = 1800  # напоминание о неясных входах panic (и сразу при старте)
NOTES_CHUNK_CHARS = 3500  # склейка алертов panic: запас до лимита Telegram 4096 (после экранирования)
_UNCLEAR_STATE = {"unclear": "исход не доказан", "closed": "закрыт по clientOrderId, жду запись",
                  "manual": "закрыть не удалось — вручную"}


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


def _untracked_msg(symbol: str, side: str, qty: float) -> str:
    """Одна строка и у reconcile, и у проверки на старте: по ней дедуп алерта."""
    return (f"позиция на бирже без сделки в журнале (ручная или второй запуск OSKAR?): "
            f"{symbol} {side} qty {qty:g}")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s[:-1] if s.endswith("Z") else s)
    except ValueError:
        return None
    # aware -> в UTC конвертацией, naive -> считаем UTC (все писатели пишут Z)
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _panic_out() -> dict:
    """Ответ прохода panic; "unclear" — входы с неясным исходом (появляется по мере нужды)."""
    return {"cancelled": 0, "closed": 0, "errors": []}


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
        self._known_runs: set[str] = set()  # run_id, подтвержденные журналом (свои сироты)
        self._reported_foreign: set[str] = set()  # "<orders|positions>:<объект>" — алерт уже был
        self._reported_kept: set[str] = set()  # id своих SL/TP-сирот, сохраненных с алертом
        self._defer = threading.local()  # буфер алертов потока (panic: Telegram после биржи)
        # panic (ревью H3, 3-й круг): закрытие сделки — одна попытка за panic (неясный сбой мог
        # дойти до биржи — повтор закрыл бы ручную часть пары); алерт об open-сделке — один раз
        self._panic_tried: set[int] = set()
        self._panic_unchecked: set[int] = set()  # SL/TP по id не проверены в этом panic (алерт был)
        self._panic_open_alerted: set[int] = set()
        # Исход входа по cid, решенный этим процессом: {ключ: поля записи} — closed (его qty
        # закрыта) или manual (решение оператора). Проверяется перед ЛЮБЫМ close_qty по cid и
        # для сделки (ревью H3, 4-й круг): повторный /panic или несохраненная meta не закрывают
        # второй раз. Не чистится: ключ cid уникален на решение, повторно закрывать его нечего.
        self._cid_done: dict[str, dict] = {}
        self._unclear_alerted: set[str] = set()  # дедуп алерта, если запись в meta не удалась
        self._unclear_reminded_at: datetime | None = None
        self.last_report: dict = {}
        self.ticks = 0
        self._stop = threading.Event()

    # ------------------------------------------------------------------ tick

    def _panic_active(self) -> bool:
        return self._state is not None and self._state.panic_at is not None

    def tick(self) -> MonitorReport:
        from oskar.core.emergency import retry_safe_mode_persist

        retry_safe_mode_persist(self._state)  # SAFE MODE не записан в meta — повтор (ревью Codex R1)
        rep = MonitorReport()
        self._guard(rep, "sweeper", lambda: self._sweep_expired(rep))
        if self._panic_active():  # panic — без обычного сопровождения и новой защиты (ревью H3)
            self._guard(rep, "panic", lambda: self._panic_tick(rep, full=True))
        else:
            self._pending_pass(rep)
            for t in self._trades("open"):
                self._guard(rep, t["decision_id"], lambda t=t: self._check_open(t, rep))
        self._guard(rep, "reconcile", lambda: self._reconcile(rep))
        self._guard(rep, "orphans", lambda: self._scan_orphans(rep, force=self._foreign_rescan_due()))
        if self._reconciler is not None:
            self._guard(rep, "closed_reconcile", lambda: self._reconciler.tick(self._now()))
        if self._drawdown is not None:
            self._guard(rep, "drawdown", self._check_drawdown)
        self._guard(rep, "unclear_remind", self._remind_unclear)
        self.ticks += 1
        self.last_report = rep.as_dict()
        return rep

    def tick_pending(self) -> MonitorReport:
        """Быстрый проход: pending-входы (исполнение лимитки -> защита) и open-сделки без
        подтвержденного SL (109420 сразу после исполнения — повтор через 5 с, а не через
        минуту полного тика; идея Codex раунд 2). При panic — неясные входы и pending по id."""
        rep = MonitorReport()
        if self._foreign_rescan_due():  # SAFE MODE снят при чужих ордерах — не ждать полного тика
            self._guard(rep, "orphans", lambda: self._scan_orphans(rep, force=True))
        if self._panic_active():
            self._guard(rep, "panic", lambda: self._panic_tick(rep, full=False))
            return rep
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
        buf = getattr(self._defer, "notes", None)
        if buf is not None:  # panic этого потока: отправка после биржи и вне замка
            buf.append(text)
            return
        self._send(text)

    def _send(self, text: str) -> None:
        if self._notify is not None:
            try:  # notify = send_text с parse_mode=HTML: «P&L», «<» из ошибки биржи — экранируем
                self._notify(html.escape(text, quote=False))
            except Exception as e:  # noqa: BLE001 - канал не влияет на учет
                logger.warning("notify failed: %s", e)

    @contextlib.contextmanager
    def deferred_notes(self):
        """Алерты ЭТОГО потока — в список, а не в Telegram (ревью Codex H2/I, 2026-10-16): panic
        шлет их сам после биржевых действий и вне _FLATTEN_LOCK. Вложенный вызов — тот же список."""
        outer = getattr(self._defer, "notes", None)
        if outer is not None:
            yield outer
            return
        notes: list[str] = []
        self._defer.notes = notes
        try:
            yield notes
        finally:
            self._defer.notes = None

    def send_notes(self, notes: list[str]) -> None:
        """Накопленные алерты — одним сообщением (недоступный Telegram — одно ожидание, не N);
        длинный список — кусками под лимит Telegram (4096)."""
        chunk: list[str] = []
        for note in notes:
            if chunk and sum(len(n) + 1 for n in chunk) + len(note) > NOTES_CHUNK_CHARS:
                self._send("\n".join(chunk))
                chunk = []
            chunk.append(note)
        if chunk:
            self._send("\n".join(chunk))

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
        if self._panic_active():  # и в гонке тика со взводом panic (ревью H3, 3-й круг)
            logger.warning("%s: panic engaged - no new protection", key)
            return
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

    def _check_open(self, t: sqlite3.Row, rep: MonitorReport, *, panic: bool = False) -> None:
        """open-сделка. panic=True (ревью H3, 3-й круг): только учет по id (сработал SL/TP, позиции
        нет) и алерт — новая защита не ставится, стоп по времени не работает."""
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
        if panic:
            cur = self._reload(t)  # тик под panic идет вне замка: panic мог закрыть ее параллельно
            if cur is not None and cur["status"] == "open":
                self._panic_open_alert(t)
            return

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
    ) -> bool:
        """Закрытие по данным монитора. reconciled=0 ВСЕГДА: «сверено» ставит только
        sync/reconcile.py по филлам биржи (ст. 1 п. 1); иначе sl/tp/duration-закрытия
        сверка не увидела бы никогда, а INV-003 навсегда осталась бы без данных.
        Только из open/pending (ревью H3, 4-й круг: учет open под panic идет вне замка flatten —
        panic и тик не перетирают закрытие друг друга). False — сделка уже закрыта другим путем."""
        now = self._now()
        opened = _parse_iso(t["opened_at"])
        duration = int((now - opened).total_seconds()) if opened else None
        pnl = None
        if exit_price is not None and t["entry_price"] is not None and t["qty"]:
            sign = 1.0 if t["side"] == "long" else -1.0
            pnl = sign * (exit_price - t["entry_price"]) * t["qty"]
            pnl -= (t["entry_fees"] or 0.0) + (exit_fee or 0.0)
        with transaction(self._j):
            cur = self._j.execute(
                "UPDATE trades SET status='closed', exit_price=?, close_reason=?, exit_fees=?, "
                "realized_pnl=?, closed_at=?, duration_seconds=?, reconciled=0 "
                "WHERE id=? AND status IN ('open','pending')",
                (exit_price, reason, exit_fee, pnl, _iso(now), duration, t["id"]),
            )
        if cur.rowcount == 0:
            logger.info("trade %s already closed - %s not booked", t["decision_id"], reason)
            return False
        rep.closed.append((t["decision_id"], reason))
        label = {"sl_hit": "🛑 стоп", "tp_hit": "🎯 тейк", "duration_timeout": "⏱ стоп по времени",
                 "manual": "✋ закрыта извне", "panic": "🚨 panic"}.get(reason, reason)
        pnl_txt = f", P&L {pnl:+.2f}" if pnl is not None else ""
        px_txt = f" @ {exit_price:.6g}" if exit_price is not None else ""
        self._say(f"{label}: {t['symbol']} {t['side']}{px_txt}{pnl_txt}")
        return True

    # ------------------------------------------------------------- reconcile

    def _exchange_positions(self) -> dict[tuple[str, str], float]:
        on_exchange: dict[tuple[str, str], float] = {}
        for p in self._ex.positions():
            if p.qty > 0:
                on_exchange[(p.symbol, p.side)] = on_exchange.get((p.symbol, p.side), 0.0) + p.qty
        return on_exchange

    def _reconcile(self, rep: MonitorReport) -> None:
        """Позиция биржи без сделки журнала (ручная, вторая машина, потерянный журнал) не
        закрывается и не игнорируется: SAFE MODE + алерт после подтверждения вторым тиком."""
        on_exchange = self._exchange_positions()
        in_journal: dict[tuple[str, str], float] = {}
        for r in self._trades("open"):
            key = (r["symbol"], r["side"])
            in_journal[key] = in_journal.get(key, 0.0) + float(r["qty"] or 0.0)
        found: list[str] = []
        for key, qty in on_exchange.items():
            if key not in in_journal:
                found.append(_untracked_msg(key[0], key[1], qty))
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

    def _run_known(self, run_id: str) -> bool:
        if run_id in self._known_runs:
            return True
        current = getattr(self._state, "run_id", None)
        try:
            known = run_known(self._j, run_id, current)
        except Exception as e:  # noqa: BLE001 - журнал не прочитан: чужой (не снимать надежнее)
            logger.warning("run %s lookup failed (%s) - treated as foreign", run_id, e)
            return False
        if known:
            self._known_runs.add(run_id)  # запуск журналу не «забывается»
        return known

    def _foreign_found(self, items: list[str], scope: str) -> None:
        """Ордера/позиции чужого запуска (вторая машина?): НИЧЕГО не снимаем — SAFE MODE и
        алерт. SAFE MODE — на КАЖДОМ скане, пока они есть (ревью Codex H2/I, 2026-10-16:
        panic-reset при оставшихся ордерах снимал защиту навсегда); дедуп — только алерта:
        новые объекты или повторный взвод после снятия — алерт, тот же набор — молча."""
        current = {f"{scope}:{x}" for x in items}
        new = current - self._reported_foreign
        self._reported_foreign = {k for k in self._reported_foreign if not k.startswith(scope + ":")}
        self._reported_foreign |= current
        if not items:
            return
        reengaged = self._state is not None and engage_safe_mode(
            self._state, "на бирже ордера/позиции OSKAR чужого запуска (вторая машина?)")
        if not new and not reengaged:
            return
        logger.critical("foreign OSKAR %s on exchange (second machine?): %s", scope, "; ".join(items))
        shown = "; ".join(items[:5]) + (f" и еще {len(items) - 5}" if len(items) > 5 else "")
        again = "" if new else "SAFE MODE взведен снова — "
        self._say(f"🛑 {again}на бирже ордера/позиции OSKAR чужого запуска (вторая машина?): {shown} — "
                  "новые входы остановлены (SAFE MODE), ничего не снимаю. Проверь, не запущен ли "
                  "oskard на второй машине; снять — oskar panic-reset")

    def _foreign_rescan_due(self) -> bool:
        """Чужие ордера были на прошлом скане, а SAFE MODE уже снят (panic-reset оператора):
        скан сразу, а не через ORPHAN_SCAN_SEC — иначе до 10 мин входы шли рядом с ордерами второй
        машины (ревью Codex H2/I, 2-й круг, 2026-10-16). Только память."""
        return (self._state is not None and not self._state.safe_mode
                and any(k.startswith("orders:") for k in self._reported_foreign))

    def _scan_orphans(self, rep: MonitorReport, force: bool = False) -> None:
        """Открытые ордера OSKAR (clientOrderId «OSKAR-...»), которых журнал не знает:
        процесс упал между отправкой входа и записью, или после закрытия сделки
        остался ее SL/TP. Сирота-вход позже открыл бы позицию мимо журнала, а
        забытый closePosition-стоп закрыл бы СЛЕДУЮЩУЮ позицию по символу — снимаем.
        Чужие ордера (ручные, прикрепленные биржей без нашего cid) не трогаем.

        Блок I, 2026-10-16: снимаем только сирот СВОИХ запусков (run_id из cid известен
        журналу, core/run_registry.py). Чужой run_id — ордер второй машины: снять его = снять
        стоп чужой позиции. Не трогаем, SAFE MODE + алерт (_foreign_found)."""
        now = self._now()
        last = self._orphan_scan_at
        if not force and last is not None and (now - last).total_seconds() < ORPHAN_SCAN_SEC:
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
        foreign: list[str] = []
        exposed: tuple[dict[str, tuple[str, str]], set[str]] | None = None
        exposed_read = False
        kept: set[str] = set()
        for o in orders:
            cid = o.client_order_id.lower()
            if not cid.startswith("oskar-"):
                continue
            if o.exchange_id in known_ids or cid_base(cid) in known_cids:
                continue
            run = cid_run_id(cid)
            if run is None or not self._run_known(run):
                foreign.append(f"{o.symbol} {o.kind} {o.client_order_id}")
                continue
            if o.kind != "entry":
                # Защита своего сироты может быть ЕДИНСТВЕННЫМ стопом позиции, потерянной журналом
                # (ревью Codex H2/I, 2026-10-16): сохраняем, только если это защита позиции без
                # сделки ПО ПАРЕ symbol/side (2-й круг: по одному символу сохранялся и стоп long при
                # ручном short — и закрыл бы следующую long-сделку по старой цене).
                if not exposed_read:
                    exposed, exposed_read = self._exposed_protection(), True
                if exposed is None or o.symbol in exposed[1]:
                    logger.warning("own orphan %s %s %s kept: %s", o.kind, o.symbol, o.exchange_id,
                                   "positions unknown" if exposed is None
                                   else "protection of untracked position not read")
                    continue
                if o.exchange_id in exposed[0]:
                    kept.add(o.exchange_id)
                    self._kept_orphan_alert(o, exposed[0][o.exchange_id])
                    continue
            self._ex.cancel_order(o.symbol, o.exchange_id)
            rep.orphans.append(f"{o.symbol}:{o.kind}:{o.exchange_id}")
            if o.kind == "entry":
                self._say(f"⚠️ снят вход-сирота {o.symbol} ({o.client_order_id}): "
                          "ордер OSKAR без сделки в журнале")
            else:
                logger.info("orphan %s %s %s cancelled (%s)", o.kind, o.symbol, o.exchange_id,
                            o.client_order_id)
        self._reported_kept &= kept  # ушла и вернулась — алерт снова
        self._foreign_found(foreign, "orders")

    def _exposed_protection(self) -> tuple[dict[str, tuple[str, str]], set[str]] | None:
        """Защита позиций биржи без open/pending сделки: ({id ордера: (symbol, side)} по
        protection_orders(symbol, side), символы, где ее прочитать не удалось). None — позиции не
        прочитаны (отсутствие позиции не подтверждено — защиту не снимаем)."""
        try:
            untracked = self._untracked_positions()
        except Exception as e:  # noqa: BLE001
            logger.warning("positions unavailable for orphan scan (%s) - own protection kept", e)
            return None
        ids: dict[str, tuple[str, str]] = {}
        unread: set[str] = set()
        for symbol, side in untracked:
            try:
                for o in self._ex.protection_orders(symbol, side):
                    ids[o.exchange_id] = (symbol, side)
            except Exception as e:  # noqa: BLE001 - не прочитали: защиту на символе не снимаем
                logger.warning("protection of untracked %s %s unavailable: %s", symbol, side, e)
                unread.add(symbol)
        return ids, unread

    def _kept_orphan_alert(self, o: OpenOrderRef, target: tuple[str, str]) -> None:
        """Свой SL/TP без сделки сохранен как защита позиции без сделки — один алерт на ордер
        (раньше только лог: оператор не знал, что ордер живет бессрочно)."""
        logger.warning("own orphan %s %s %s kept: protection of untracked position %s %s",
                       o.kind, o.symbol, o.exchange_id, *target)
        if o.exchange_id in self._reported_kept:
            return
        self._reported_kept.add(o.exchange_id)
        self._say(f"⚠️ SL/TP OSKAR без сделки сохранен: {target[0]} {target[1]} {o.kind} "
                  f"({o.client_order_id}) — на бирже позиция без сделки в журнале, это может быть "
                  "ее единственный стоп. Позиция не OSKAR — сними ордер вручную")

    # ---------------------------------------------------------------- startup

    def startup_check(self) -> MonitorReport:
        """Старт демона, ДО запуска runner'а (первого входа): позиции биржи без сделки в журнале
        (SAFE MODE + алерт; ничего не закрываем, учет не восстанавливаем), затем скан сирот (свои —
        снять, кроме защиты на паре с позицией без сделки; чужого запуска — SAFE MODE, не трогать),
        затем напоминание о неясных входах panic. Позиции — ПЕРВЫМИ (ревью Codex H2/I, 2026-10-16):
        скан сирот снимал единственный SL потерянной сделки до сверки позиций.
        Биржа недоступна — не приговор: первый полный тик монитора повторит скан."""
        rep = MonitorReport()
        self._guard(rep, "startup_positions", lambda: self._startup_positions(rep))
        try:
            self._scan_orphans(rep, force=True)
        except Exception as e:  # noqa: BLE001
            self._orphan_scan_at = None  # первый тик повторит
            rep.errors.append(f"orphans: {type(e).__name__}: {e}")
            logger.warning("startup orphan scan failed: %s", e)
        self._guard(rep, "unclear_remind", lambda: self._remind_unclear(force=True))
        logger.info("startup check: %s", rep.as_dict())
        return rep

    def _untracked_positions(self) -> dict[tuple[str, str], float]:
        """Позиции биржи без open/pending сделки журнала (pending: лимитка могла исполниться,
        пока демон лежал — ее переведет первый тик)."""
        on_exchange = self._exchange_positions()
        tracked = {(r["symbol"], r["side"]) for r in self._j.execute(
            "SELECT symbol, side FROM trades WHERE status IN ('open','pending')").fetchall()}
        return {k: q for k, q in on_exchange.items() if k not in tracked}

    def _startup_positions(self, rep: MonitorReport) -> None:
        """Позиция без сделки при старте: вторая машина, ручная или вход OSKAR, упавший до записи.
        Тот же исход, что у reconcile (SAFE MODE + алерт, ничего не закрываем), но до первого входа.
        Учет НЕ восстанавливается (ревью Codex H3, 3-й круг: восстановление присваивало ручную
        позицию той же пары): алерт несет данные позиции и ордера OSKAR на ней, решение — за
        оператором. Подтверждение повторным чтением (один кривой ответ биржи — не расхождение)."""
        first = self._untracked_positions()
        if not first:
            return
        confirmed = {k: q for k, q in self._untracked_positions().items() if k in first}
        if not confirmed:
            return
        entry: dict[tuple[str, str], float] = {}
        for p in self._ex.positions():
            if (p.symbol, p.side) in confirmed and p.qty > 0:
                entry.setdefault((p.symbol, p.side), p.entry_price)
        for (symbol, side), qty in confirmed.items():
            m = _untracked_msg(symbol, side, qty)
            # reconcile полного тика не повторяет тот же алерт
            self._mismatch_streak[m] = CONFIRM_TICKS
            self._reported_mismatches.add(m)
            rep.mismatches.append(m)
            self._untracked_alert(symbol, side, qty, entry.get((symbol, side)))

    def _untracked_alert(self, symbol: str, side: str, qty: float, entry: float | None) -> None:
        """SAFE MODE и алерт: позиция, ордера OSKAR на этой паре и что делать вручную."""
        try:
            own = [o for o in self._ex.protection_orders(symbol, side)
                   if o.status == "open" and o.client_order_id.lower().startswith("oskar-")]
        except Exception as e:  # noqa: BLE001 - алерт важнее списка
            orders = f"не прочитаны ({short_error(e)})"
        else:
            self._reported_kept.update(o.exchange_id for o in own)  # скан сирот не дублирует алерт
            orders = "; ".join(
                f"{o.kind.upper()} {o.client_order_id}"
                + (f" @ {o.trigger_price:.6g}" if o.trigger_price else "") for o in own) or "нет"
        if self._state is not None:
            engage_safe_mode(self._state, f"позиция {symbol} {side} без сделки в журнале")
        px = f" @ {entry:.6g}" if entry else ""
        self._say(f"🛑 {_untracked_msg(symbol, side, qty)}{px}. Ордера OSKAR на этой паре: {orders}. "
                  "Ничего не закрываю и не снимаю, учет не восстанавливаю; новые входы остановлены "
                  "(SAFE MODE). Если это вход OSKAR, упавший до записи, — сопровождай или закрой "
                  "позицию вручную на бирже (SL/TP OSKAR оставлены на месте), затем oskar panic-reset")

    # ----------------------------------------------------------------- panic
    #
    # Ревью Codex H3, 3-й круг (2026-10-16): закрывается только экспозиция, доказанная своей по
    # идентификаторам — open-сделка журнала (ее qty) и вход, исполнение которого биржа подтвердила
    # по id ордера / clientOrderId (его qty). Остальное — список неясных входов (meta, переживает
    # рестарт), SAFE MODE и один алерт; по symbol/side «на всякий случай» не закрывается ничего.

    def panic_begin(self) -> None:
        """engage_panic: новый panic — закрытие сделок снова разрешено (по одной попытке).
        Решенные по cid входы (_cid_done) не сбрасываются: второе закрытие задело бы ручную часть."""
        self._panic_tried.clear()
        self._panic_unchecked.clear()

    def panic_flatten(self) -> dict:
        """Ст. 6: снять входы OSKAR и закрыть его позиции. Флаги уже стоят — сюда после них.
        Ошибка по одной сделке не останавливает остальные."""
        rep = MonitorReport()
        out = _panic_out()
        items = unclear_entries(self._j)
        for t in self._trades("pending") + self._trades("open"):
            self._panic_trade(t, rep, out, items)
        # сводку шлет engage_panic — после финального прохода и вне замка (ревью Codex H2/I)
        logger.warning("panic flatten pass: cancelled %d, closed %d, errors %d, unclear %d",
                       out["cancelled"], out["closed"], len(out["errors"]), len(out.get("unclear", [])))
        return out

    def _panic_trade(self, t: sqlite3.Row, rep: MonitorReport, out: dict, items: dict) -> None:
        """Сделка журнала при panic. Неясный вход уже закрыт по clientOrderId (state closed) — только
        учет; закрыть его не удалось (manual) — не трогаем, решение оператора; иначе open — закрыть
        ее qty, pending — исход по id. Состояние, решенное этим процессом (_cid_done), — главнее
        meta: запись могла не сохраниться (ревью H3, 4-й круг)."""
        key = cid_base(str(t["client_order_id"]))
        state = (self._cid_done.get(key) or items.get(key) or {}).get("state")
        try:
            if state == "closed":
                self._book_closed_by_cid(t, rep, key)
            elif state == "manual":  # закрыть не удалось — решение оператора, напоминания
                logger.debug("panic: %s left to operator (close failed before)", t["decision_id"])
            elif t["status"] == "open":
                self._panic_close_open(t, rep, out)
            else:
                self._panic_pending(t, rep, out)
        except Exception as e:  # noqa: BLE001 - одна сделка не валит проход
            out["errors"].append(f"{t['decision_id']}: {type(e).__name__}: {e}")

    def _panic_pending(self, t: sqlite3.Row, rep: MonitorReport, out: dict) -> None:
        """pending-вход — исход ПО ID (entry_order_id или clientOrderId): исполнен целиком или
        частично — остаток снять, закрыть ровно исполненную qty, защиту снять по id, сделка
        closed/panic; снят без исполнения или его нет на бирже за UNKNOWN_ENTRY_TTL_SEC — cancelled;
        не доказано — запись остается pending, вход в список неясных (SAFE MODE, алерт)."""
        kind, st, why = self._resolve_entry(t["symbol"], lambda: self._entry_state(t))
        if kind == "missing":
            if self._age_sec(t) >= UNKNOWN_ENTRY_TTL_SEC:
                kind = "none"
            else:
                kind, why = "unknown", "ордер еще не виден на бирже (вход после таймаута)"
        key = cid_base(str(t["client_order_id"]))
        if kind == "filled":
            self._promote(t, st, rep, partial=not st.is_filled)  # учет — по бирже
            self._panic_close_open(self._reload(t), rep, out)
            return
        if kind == "none":
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
            self._unclear_resolved(key)
            return
        self._mark_unclear(key, self._item_for_trade(t), why, out)

    def _panic_close_open(self, t: sqlite3.Row, rep: MonitorReport, out: dict) -> bool:
        """Закрыть ровно qty сделки (close_qty: ручная часть той же пары остается) и снять ее защиту
        по id; учет — closed/panic. Одна попытка за panic: сбой мог дойти до биржи, а повтор закрыл бы
        ручную часть пары — алерт, решение за оператором (повторный /panic пробует снова).

        Ревью H3, 4-й круг (P2-1): до close_qty — SL/TP сделки по id (журнал узнает о сработавшем
        стопе только на полном тике, а close_qty после него срезал бы ручную часть пары). Сработал —
        учет sl_hit/tp_hit без закрытия; проверить не удалось — не закрываем (не доказано, что
        позиция еще своя): SAFE MODE и один алерт за panic, на бирже ничего не сделано — повтор
        (финальный проход, тик неясного входа, повторный /panic) безопасен."""
        did, key = t["decision_id"], cid_base(str(t["client_order_id"]))
        if t["id"] in self._panic_tried:
            return False
        cur = self._reload(t)
        if cur is None or cur["status"] != "open":
            return False  # учтена другим путем (тик под panic идет вне замка flatten)
        t = cur
        try:
            if self._close_if_protection_filled(t, rep):  # учет sl_hit/tp_hit, парный ордер снят
                logger.warning("panic: %s protection already filled - booked, nothing closed", did)
                self._unclear_resolved(key)
                return False
        except Exception as e:  # noqa: BLE001 - не проверили — не закрываем
            if t["id"] not in self._panic_unchecked:
                self._panic_unchecked.add(t["id"])
                self._panic_open_alerted.add(t["id"])  # тик не дублирует алерт об open-сделке
                out["errors"].append(f"{did}: SL/TP по id не проверены: {short_error(e)}")
                self._hold_safe_mode(f"panic: {t['symbol']} {t['side']} ({did}) SL/TP не проверены")
                self._say(f"🛑 panic: {t['symbol']} {t['side']} qty {float(t['qty'] or 0):g} ({did}) "
                          f"не закрыта — SL/TP по id не проверены ({short_error(e)}): сработавший стоп "
                          "+ закрытие срезали бы ручную часть пары. Повторю на финальном проходе; не "
                          "выйдет — закрой вручную или повтори /panic (SAFE MODE)")
            return False
        self._panic_tried.add(t["id"])
        qty = float(t["qty"] or 0)
        try:
            order = self._ex.close_qty(t["symbol"], t["side"], qty)
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{did}: {short_error(e)}")
            self._panic_open_alerted.add(t["id"])
            self._mark_manual(key, f"закрыть не удалось: {short_error(e)}")
            self._hold_safe_mode(f"panic: {t['symbol']} {t['side']} ({did}) закрыть не удалось")
            self._say(f"🛑 panic: {t['symbol']} {t['side']} qty {qty:g} ({did}) закрыть не удалось "
                      f"({short_error(e)}) — закрой вручную; сам не повторяю (запрос мог дойти до биржи)")
            return False
        # qty закрыта — сначала память: сбой учета ниже не приведет ко второму закрытию (4-й круг)
        self._cid_done[key] = {"state": "closed", "closed_qty": qty}
        out["errors"].extend(f"{did}: {e}" for e in self._cancel_own_protection(
            t["symbol"], t["side"], key, qty or None, t["sl_price"], t["tp_price"],
            (t["sl_order_id"], t["tp_order_id"])))
        exit_price, fee = None, None
        if order is not None and order.exchange_id:
            try:
                st = self._ex.order_status(t["symbol"], exchange_id=order.exchange_id)
                exit_price, fee = st.avg_price, st.fee
            except Exception as e:  # noqa: BLE001
                logger.warning("panic close lookup failed for %s: %s", did, e)
        self._close(t, rep, exit_price, "panic", fee)
        out["closed"] += 1
        self._unclear_resolved(key)
        return True

    def _book_closed_by_cid(self, t: sqlite3.Row, rep: MonitorReport, key: str) -> None:
        """Неясный вход уже закрыт по clientOrderId, а исполнитель записал сделку позже: только учет
        и снятие защиты по id — второе закрытие задело бы ручную часть пары."""
        qty = float(t["qty"] or 0) or None
        for err in self._cancel_own_protection(t["symbol"], t["side"], key, qty, t["sl_price"],
                                               t["tp_price"], (t["sl_order_id"], t["tp_order_id"])):
            logger.warning("panic: protection of %s: %s", t["decision_id"], err)
        self._close(t, rep, None, "panic", None)
        self._unclear_resolved(key)

    def panic_resolve_inflight(self, entries: dict) -> dict:
        """Входы, не завершенные за окно ожидания panic (ревью Codex R1; H3, 3-й круг): записи в
        журнале нет — исход по clientOrderId. Исполнение доказано — закрыть ровно его qty, защиту
        снять по id; не доказано — список неясных, SAFE MODE, алерт. По symbol/side не закрывается
        ничего. entries — {база cid OSKAR-<run>-<hash>: (symbol, side) | None}.

        Повторный /panic (ревью H3, 4-й круг: второй канал или повторное нажатие, исполнитель еще в
        процессе) берет СОХРАНЕННУЮ запись, а не новую unclear: closed — его qty уже закрыта, второго
        закрытия нет; manual — не трогаем, решение оператора (вход — в сводку неясных)."""
        from oskar.core.executor import inflight_client_ids, inflight_decision

        out = _panic_out()
        rep = MonitorReport()
        inflight = inflight_client_ids()
        try:
            stored = unclear_entries(self._j)
        except Exception as e:  # noqa: BLE001 - закрытое этим процессом помнит _cid_done
            logger.error("panic unclear entries unreadable: %s", e)
            stored = {}
        for key, target in entries.items():
            if target is None:
                continue  # план не прочитан — допуска к отправке не было
            known = stored.get(key)
            item = (dict(known) if known is not None
                    else self._new_item(key, target[0], target[1], inflight_decision(key)))
            try:
                why = self._step_entry(key, item, inflight, rep, out, fresh=True)
            except Exception as e:  # noqa: BLE001 - не проверили — исход неясен, а не «нет»
                why = f"проверка по clientOrderId упала: {type(e).__name__}: {e}"
            if why is not None:
                self._mark_unclear(key, item, why, out)
            elif known is not None and known.get("state") == "manual":
                out.setdefault("unclear", []).append(item.get("decision_id") or key)
        return out

    def _step_entry(self, key: str, item: dict, inflight: set[str], rep: MonitorReport, out: dict,
                    *, fresh: bool = False) -> str | None:
        """Неясный вход panic: запись списка. fresh — вызов из engage_panic для входа, не
        завершенного за окно: записи в meta может еще не быть (сохранение — upsert), а сделку,
        записанную исполнителем только что, ведем сразу. Возвращает причину, если исход по-прежнему
        не доказан.

        Сделка уже в журнале — ее ведет путь сделок panic (_panic_trade) с состоянием этой записи.
        Иначе — исход по clientOrderId, в том числе пока исполнитель в процессе (ревью Codex H3,
        2-й круг: биржа могла принять вход, а исполнитель еще ждет ответа). Исполнение доказано —
        закрыть ровно его qty; исполнитель запишет сделку позже — запись остается (state closed),
        учет без второго закрытия. Ордера нет за UNKNOWN_ENTRY_TTL_SEC и исполнитель завершен —
        входа нет. Исход, решенный этим процессом (_cid_done), главнее записи: meta могла не
        сохраниться, а второй close_qty срезал бы ручную часть пары (ревью H3, 4-й круг)."""
        t = self._trade_by_key(key)
        if t is not None:
            if t["status"] not in ("pending", "open"):
                self._unclear_resolved(key)
            elif fresh:  # записан после финального прохода — путь сделок с состоянием записи
                self._panic_trade(t, rep, out, {key: item})
            return None
        done = self._cid_done.get(key)
        if done is not None and item.get("state", "unclear") != done["state"]:
            self._put_item(key, item, fresh, **done)  # запись отстала от памяти — догоняет
        state = item.get("state", "unclear")
        if state != "unclear":
            if state == "closed" and key not in inflight:
                self._unclear_resolved(key)  # исполнитель завершен без записи — учитывать нечего
            return None
        symbol, side = item["symbol"], item["side"]
        label = item.get("decision_id") or key
        kind, st, why = self._resolve_entry(
            symbol, lambda: self._ex.order_status(symbol, client_order_id=item["cid"]))
        if kind == "missing":
            since = _parse_iso(item.get("since")) or self._now()
            aged = (self._now() - since).total_seconds() >= UNKNOWN_ENTRY_TTL_SEC
            if key not in inflight and aged:
                kind = "none"
            else:
                kind, why = "unknown", ("исполнитель еще ждет ответа биржи" if key in inflight
                                        else "ордер не найден по clientOrderId")
        if kind == "none":
            if key in inflight:
                return "вход снят биржей, исполнитель еще в процессе"
            # алерт — только если запись была (только что не завершенный вход без записи — молча)
            self._unclear_resolved(key, f"✅ panic: вход {label} ({symbol} {side}) не исполнен — "
                                        "снят со списка неясных")
            return None
        if kind == "unknown":
            return why
        qty = float(st.filled_qty or 0)  # исполнен — доказано по clientOrderId
        if qty <= 0:
            self._settle(key, item, fresh, state="manual", why="исполнен, объем биржа не отдала")
            self._hold_safe_mode(f"panic: вход {label} ({symbol} {side}) исполнен, объем неизвестен")
            out["errors"].append(f"{label}: исполнен, объем неизвестен")
            self._say(f"🛑 panic: вход {label} исполнен, но объем биржа не отдала — закрой {symbol} "
                      f"{side} вручную (сам по паре не закрываю)")
            return None
        try:
            order = self._ex.close_qty(symbol, side, qty)
        except Exception as e:  # noqa: BLE001
            self._settle(key, item, fresh, state="manual", why=f"закрыть не удалось: {short_error(e)}")
            self._hold_safe_mode(f"panic: вход {label} ({symbol} {side}) закрыть не удалось")
            out["errors"].append(f"{label}: {short_error(e)}")
            self._say(f"🛑 panic: вход {label} исполнен после окна (qty {qty:g}), закрыть не удалось "
                      f"({short_error(e)}) — закрой {symbol} {side} qty {qty:g} вручную; сам не "
                      "повторяю (запрос мог дойти до биржи)")
            return None
        # qty закрыта — сначала память (ревью H3, 4-й круг): сбой записи meta ниже не приведет ко
        # второму close_qty ни на тике, ни при повторном /panic
        self._cid_done[key] = {"state": "closed", "closed_qty": qty}
        errors = self._cancel_own_protection(symbol, side, key, qty, item.get("sl"), item.get("tp"), ())
        out["errors"].extend(f"{label}: {e}" for e in errors)
        if order is not None:
            out["closed"] += 1
        logger.critical("panic: entry %s filled after window - closed qty %g by clientOrderId (%s)",
                        key, qty, "done" if order is not None else "no position")
        what = (f"закрыт ровно его объем {symbol} {side} qty {qty:g}" if order is not None
                else f"позиции {symbol} {side} уже нет — закрывать нечего")
        self._say(f"🚨 panic: вход {label} исполнен после окна ожидания (по clientOrderId) — {what}; "
                  f"защита снята по id{f', ошибок {len(errors)}' if errors else ''}")
        if key in inflight:  # исполнитель запишет сделку позже — учет без второго закрытия
            self._put_item(key, item, fresh, **self._cid_done[key])
        else:
            self._unclear_resolved(key)
        return None

    def _resolve_entry(self, symbol: str, lookup) -> tuple[str, OrderState | None, str]:
        """Исход входа ПО ИДЕНТИФИКАТОРУ (lookup — по id биржи или clientOrderId); живой ордер
        снимается и перечитывается. ("filled", st) — исполнен целиком или частично, остаток снят;
        ("none", st) — снят без исполнения; ("missing", None) — биржа его не знает; ("unknown", st,
        причина) — не доказано (сеть, остаток не снят, статус после отмены не финальный)."""
        try:
            st = lookup()
        except OrderNotFoundError:
            return "missing", None, "ордер не найден на бирже"
        except Exception as e:  # noqa: BLE001 - не дозвонились: исход неизвестен, а не «нет»
            return "unknown", None, f"биржа не ответила: {short_error(e)}"
        if not st.is_filled and not st.is_gone:  # жив — снять остаток и перечитать
            try:
                if not self._ex.cancel_order(symbol, st.exchange_id):
                    raise RuntimeError(f"entry {st.exchange_id!r} not cancelled")
                st = self._ex.order_status(symbol, exchange_id=st.exchange_id)
            except Exception as e:  # noqa: BLE001
                return "unknown", st, f"остаток входа не снят или не перепроверен: {short_error(e)}"
            if not st.is_filled and not st.is_gone:
                return "unknown", st, f"после отмены статус входа {st.status}"
        if st.is_filled or st.filled_qty > 0:
            return "filled", st, ""
        return "none", st, ""

    def _cancel_own_protection(self, symbol: str, side: str, key: str, qty: float | None,
                               sl: float | None, tp: float | None, ids) -> list[str]:
        """Снять защиту входа ПО ID: id из журнала, ордера с базой его clientOrderId (-sl/-tp/-rN) и
        прикрепленные биржей без cid, если они его (owns_protection: объем покрывает qty, триггер —
        уровень плана; уровня нет — не трогаем). Ручные SL/TP той же пары остаются. -> ошибки."""
        errors: list[str] = []
        done: set[str] = set()
        for oid in ids:
            if not oid:
                continue
            try:
                self._ex.cancel_order(symbol, oid)
                done.add(oid)
            except Exception as e:  # noqa: BLE001
                errors.append(f"cancel {oid}: {short_error(e)}")
        try:
            orders = [o for o in self._ex.protection_orders(symbol, side) if o.status == "open"]
        except Exception as e:  # noqa: BLE001
            return [*errors, f"protection list: {short_error(e)}"]
        for o in orders:
            if o.exchange_id in done:
                continue
            if o.client_order_id:
                mine = cid_base(o.client_order_id) == key
            else:
                target = sl if o.kind == "sl" else tp
                mine = target is not None and owns_protection(
                    o, owner_cid=key, position_qty=qty, target_price=target)
            if not mine:
                continue
            try:
                self._ex.cancel_order(symbol, o.exchange_id)
            except Exception as e:  # noqa: BLE001
                errors.append(f"cancel {o.exchange_id}: {short_error(e)}")
        return errors

    # ------------------------------------------- panic: список неясных входов (meta)

    def _trade_by_key(self, key: str) -> sqlite3.Row | None:
        for r in self._j.execute(
            "SELECT * FROM trades WHERE lower(client_order_id) LIKE ? ORDER BY id DESC", (key + "%",)
        ).fetchall():
            if cid_base(str(r["client_order_id"])) == key:
                return r
        return None

    def _new_item(self, key: str, symbol: str, side: str, decision_id: str | None) -> dict:
        """Запись неясного входа без сделки: уровни SL/TP — из плана решения (снятие его
        прикрепленной защиты по owns_protection); план не прочитан — только по cid."""
        sl = tp = None
        if decision_id:
            try:
                row = self._j.execute("SELECT card_json FROM pending_approvals WHERE decision_id = ?",
                                      (decision_id,)).fetchone()
                if row is not None:
                    plan = ExecutionPlan.model_validate_json(row["card_json"])
                    sl, tp = plan.sl, plan.tp
            except Exception as e:  # noqa: BLE001
                logger.warning("plan of %s unreadable for panic item: %s", decision_id, e)
        cid = "OSKAR" + key[5:] if key.startswith("oskar") else key  # make_client_order_id: OSKAR-<run>-<hex>
        return {"cid": cid, "decision_id": decision_id, "symbol": symbol, "side": side, "sl": sl,
                "tp": tp, "since": _iso(self._now()), "state": "unclear"}

    def _item_for_trade(self, t: sqlite3.Row) -> dict:
        return {"cid": str(t["client_order_id"]), "decision_id": t["decision_id"], "symbol": t["symbol"],
                "side": t["side"], "sl": t["sl_price"], "tp": t["tp_price"], "since": _iso(self._now()),
                "state": "unclear"}

    def _mark_unclear(self, key: str, item: dict, why: str, out: dict) -> None:
        """Исход входа не доказан: в список неясных (meta), SAFE MODE на обоих носителях и ОДИН
        алерт на вход. По symbol/side ничего не закрываем."""
        label = item.get("decision_id") or key
        out.setdefault("unclear", []).append(label)
        lost = ""
        try:
            new = add_unclear_entry(self._j, key, {**item, "why": why}, self._state)
        except Exception as e:  # noqa: BLE001 - алерт важнее записи
            logger.error("panic unclear entry %s not saved: %s", key, e)
            new = key not in self._unclear_alerted
            lost = " Запись в журнал не удалась — после рестарта не вспомню."
        self._unclear_alerted.add(key)
        if not new:
            return
        self._hold_safe_mode(f"panic: вход {label} ({item['symbol']} {item['side']}) с неясным исходом")
        self._unclear_reminded_at = self._now()
        self._say(f"🛑 panic: вход {label} с неясным исходом ({why}) — проверь позицию {item['symbol']} "
                  f"{item['side']} вручную. По symbol/side ничего не закрываю; вход проверяю по "
                  f"clientOrderId на каждом тике. Новые входы остановлены (SAFE MODE).{lost}")

    def _hold_safe_mode(self, reason: str) -> None:
        """SAFE MODE с причиной на обоих носителях и при panic (тот держит его только в памяти)."""
        if self._state is not None:
            hold_safe_mode(self._state, reason)

    def _mark_manual(self, key: str, why: str) -> None:
        """Закрытие сделки неясного входа не удалось — запись больше не ведет автоматику."""
        try:
            update_unclear_entry(self._j, key, state="manual", why=why)
        except Exception as e:  # noqa: BLE001
            logger.error("panic unclear entry %s not updated: %s", key, e)

    def _put_item(self, key: str, item: dict, fresh: bool, **fields) -> None:
        """Поля записи неясного входа. fresh — upsert (ревью H3, 4-й круг: insert не обновлял уже
        сохраненный ключ — после повторного /panic state оставался unclear, и тик закрывал вход
        второй раз); иначе — только существующая запись (снятая reset'ом не воскресает)."""
        item.update(fields)
        try:
            if fresh:
                upsert_unclear_entry(self._j, key, item, self._state)
                self._unclear_reminded_at = self._now()  # алерт об исходе уже ушел
            else:
                update_unclear_entry(self._j, key, **fields)
        except Exception as e:  # noqa: BLE001 - исход помнит _cid_done; повтор записи — с тика
            logger.error("panic unclear entry %s not saved: %s", key, e)

    def _settle(self, key: str, item: dict, fresh: bool, **fields) -> None:
        """Исход входа решен этим процессом (manual — решение оператора): сначала память, затем
        meta — несохраненная запись не вернет вход в автоматику ни на тике, ни при повторном /panic."""
        self._cid_done[key] = dict(fields)
        self._put_item(key, item, fresh, **fields)

    def _unclear_resolved(self, key: str, note: str | None = None) -> None:
        try:
            had = drop_unclear_entry(self._j, key)
        except Exception as e:  # noqa: BLE001 - следующий тик снимет
            logger.error("panic unclear entry %s not dropped: %s", key, e)
            return
        self._unclear_alerted.discard(key)
        if had:
            logger.warning("panic unclear entry %s resolved", key)
            if note:
                self._say(note)

    def _remind_unclear(self, force: bool = False) -> None:
        """Напоминание о неясных входах: при старте и раз в UNCLEAR_REMIND_SEC."""
        items = unclear_entries(self._j)
        if not items:
            self._unclear_reminded_at = None
            return
        now = self._now()
        at = self._unclear_reminded_at
        if not force and at is not None and (now - at).total_seconds() < UNCLEAR_REMIND_SEC:
            return
        self._unclear_reminded_at = now
        rows = [f"{v.get('decision_id') or k} {v.get('symbol')} {v.get('side')} — "
                f"{_UNCLEAR_STATE.get(v.get('state'), v.get('state'))}" for k, v in sorted(items.items())]
        shown = "; ".join(rows[:5]) + (f" и еще {len(rows) - 5}" if len(rows) > 5 else "")
        self._say(f"🛑 panic: входы с неясным исходом ({len(rows)}): {shown}. Проверь позиции вручную; "
                  "по symbol/side ничего не закрываю, вход проверяю по clientOrderId. Снять список — "
                  "oskar panic-reset")

    # ------------------------------------------------------- panic: тик монитора

    def _panic_tick(self, rep: MonitorReport, *, full: bool) -> None:
        """Тик при взведенном panic, в том числе восстановленном после рестарта (ревью Codex H3,
        2-й круг, P1-4: поздний fill шел в обычное сопровождение и получал защиту). Неясные входы —
        по id; pending — исход по id; open неясного входа — закрыть ее qty (одна попытка); прочие
        open (полный тик) — учет по id ордеров защиты и алерт. Новая защита не ставится.

        Ревью H3, 4-й круг (P2-2): под _FLATTEN_LOCK (без ожидания: занят — engage_panic снимает
        сам) — только то, что может закрыть по id, и по одному элементу: engage_panic ждет замок —
        тик уступает его после текущего элемента. Учет прочих open — после, ВНЕ замка: на бирже он
        ничего не закрывает, а /panic не ждет его сетевых вызовов (раньше — весь полный тик)."""
        from oskar.core.emergency import _FLATTEN_LOCK, flatten_wanted

        if flatten_wanted() or not _FLATTEN_LOCK.acquire(blocking=False):
            return
        notes: list[str] = []
        try:
            with self.deferred_notes() as notes:
                try:
                    own = self._panic_locked_pass(rep)
                finally:
                    _FLATTEN_LOCK.release()
        finally:
            self.send_notes(notes)  # Telegram — вне замка (и при сбое прохода)
        if own is None or not full:
            return
        notes = []
        try:
            with self.deferred_notes() as notes:
                for t in self._trades("open"):
                    if flatten_wanted() or _FLATTEN_LOCK.locked():
                        break  # panic идет — open-сделки ведет он
                    if cid_base(str(t["client_order_id"])) in own:
                        continue  # ведет проход под замком
                    self._guard(rep, t["decision_id"], lambda t=t: self._panic_check_open(t, rep))
        finally:
            self.send_notes(notes)

    def _panic_locked_pass(self, rep: MonitorReport) -> set[str] | None:
        """Часть тика под panic под _FLATTEN_LOCK: неясные входы, pending, open неясного входа или
        решенного по cid. -> ключи, которые она ведет; None — engage_panic ждет замок, проход
        прерван (оставшееся сделает он)."""
        from oskar.core.emergency import flatten_wanted
        from oskar.core.executor import inflight_client_ids

        out = _panic_out()
        try:
            # реестр — ДО журнала: исполнитель пишет сделку раньше, чем выходит из реестра
            inflight = inflight_client_ids()
            for key, item in unclear_entries(self._j).items():
                if flatten_wanted():
                    return None
                self._guard(rep, f"panic {key}",
                            lambda key=key, item=item: self._step_entry(key, item, inflight, rep, out))
            items = unclear_entries(self._j)
            own = set(items) | set(self._cid_done)
            for t in self._trades("pending"):
                if flatten_wanted():
                    return None
                self._guard(rep, t["decision_id"], lambda t=t: self._panic_trade(t, rep, out, items))
            for t in self._trades("open"):
                if cid_base(str(t["client_order_id"])) not in own:
                    continue
                if flatten_wanted():
                    return None
                self._guard(rep, t["decision_id"], lambda t=t: self._panic_trade(t, rep, out, items))
            return own
        finally:
            rep.errors.extend(out["errors"])

    def _panic_check_open(self, t: sqlite3.Row, rep: MonitorReport) -> None:
        cur = self._reload(t)  # проход вне замка: panic мог закрыть ее, пока шли сетевые вызовы
        if cur is not None and cur["status"] == "open":
            self._check_open(cur, rep, panic=True)

    def _panic_open_alert(self, t: sqlite3.Row) -> None:
        if t["id"] in self._panic_open_alerted:
            return
        self._panic_open_alerted.add(t["id"])
        self._say(f"🛑 panic взведен, а сделка {t['decision_id']} {t['symbol']} {t['side']} qty "
                  f"{float(t['qty'] or 0):g} открыта — новую защиту не ставлю, по времени не закрываю. "
                  "Закрой вручную или повтори /panic (SAFE MODE)")

    # ---------------------------------------------------------------- thread

    def _has_pending(self) -> bool:
        # pending-входы ИЛИ open без подтвержденного SL (перепроверка Codex: при одной open
        # без стопа быстрый проход не запускался — повтор ждал полного тика); ИЛИ скан чужих
        # ордеров после снятия SAFE MODE (ревью Codex H2/I, 2-й круг); ИЛИ неясные входы panic
        # (ревью H3, 3-й круг) — проверка по id каждые 5 с, а не раз в минуту
        if self._foreign_rescan_due():
            return True
        if self._panic_active() and unclear_entries(self._j):
            return True
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
