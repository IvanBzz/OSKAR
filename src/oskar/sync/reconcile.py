"""Сверка ЗАКРЫТЫХ сделок с историей исполнений биржи (CONSTITUTION.md ст. 1 п. 1-2).

Ревью 2026-10-13 (Claudexor H10, Codex #12): закрытые сделки никогда не сверялись
с биржей — P&L считался из наших же чисел (плановая цена при сбое lookup, нулевая
комиссия вместо неизвестной), а закрытия manual/panic оставались с exit/P&L = NULL
навсегда. Журнал — кеш; правда — филлы биржи (ccxt fetch_my_trades). Порядок:

1. Выход ищется сначала ТОЧНО (ст. 1 п. 2): по биржевым id SL/TP сделки и по
   clientOrderId (`<cid>-sl`, `<cid>-tp`, `<cid>-sl-rN`). BingX может вернуть
   clientOrderID в нижнем регистре — сравнение без учета регистра.
2. Точные филлы не покрыли qty (manual/panic/duration: рыночное закрытие несет пустой
   clientOrderId) -> окно: закрывающая сторона + positionSide сделки в
   [opened_at - skew, closed_at + grace]. Окно режется соседними сделками того же
   символа и стороны; филлы, точно принадлежащие другим сделкам, и уже разобранные
   в этом прогоне не берутся.
3. Вход: точные филлы (entry_order_id, если колонка есть, или clientOrderId) ->
   иначе журнал, если в нем есть И цена, И комиссия входа (их писал order_status
   биржи) -> иначе окно открывающей стороны. Вход неизвестен -> не сверено: P&L без
   комиссии входа и есть тот самый «выдуманный» P&L.
4. exit_price/exit_fees/realized_pnl (+ подтвержденный вход) и reconciled=1 пишутся
   ТОЛЬКО когда филлы выхода покрывают qty сделки в пределах 1%; иначе reconciled
   остается 0, а сделка — в отчете.

Один прогон = одна строка sync_snapshots(kind='reconcile') с результатом по каждой
сделке (старые значения журнала — в diffs: аудит «журнал врал»); clean=1 — все
сверено и журнал ни в чем не противоречил бирже. Эти строки читает INV-003
(ops/invariants.py, reconcile_diff_rows).

ClosedTradeReconciler — расписание ст. 1 п. 1: сразу после закрытой несверенной
сделки (повтор не чаще раза в 10 минут — филлы попадают в историю с задержкой),
далее каждые 6 часов. Тик дешевый (один SELECT): его зовет монитор позиций.
"""

import html
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from oskar.journal.db import transaction

logger = logging.getLogger("oskar.reconcile")

QTY_TOLERANCE = 0.01  # филлы выхода покрывают qty сделки в пределах 1% (округления биржи)
ENTRY_SKEW = timedelta(seconds=120)  # opened_at пишется ПОСЛЕ филла входа; плюс дрейф часов Windows
EXIT_GRACE = timedelta(seconds=300)  # closed_at — момент, когда монитор ЗАМЕТИЛ закрытие, не филл
HISTORY_LIMIT = timedelta(days=29)  # BingX allFillOrders не отдает глубже 30 дней
DEFAULT_INTERVAL_SEC = 6 * 3600  # ст. 1 п. 1: «далее каждые 6 часов»
RETRY_SEC = 600  # несверенную сделку повторяем не чаще раза в 10 минут...
FAST_RETRIES = 6  # ...первый час; дальше — только 6-часовым прогоном (без снепшота каждые 10 мин)
# qty филлов — в монетах, как qty журнала. У линейных перпов BingX contractSize=1, но qty-баг
# v1 был «contractSize-shaped» (ratio 6.98x-833x) — единицы задаем явно, не полагаемся на дефолт.
FETCH_PARAMS = {"tradingUnit": "COIN"}

# Допуски «журнал vs биржа» (относительный, абсолютный): старое НЕ-NULL значение журнала
# дальше допуска = журнал врал (diff). NULL -> значение — не расхождение: монитор честно
# не знал (manual/panic).
_PRICE_TOL = (1e-4, 1e-12)
_FEE_TOL = (1e-3, 1e-6)
_PNL_TOL = (1e-3, 0.01)

STATUS_OK = "reconciled"  # сверено, журнал не противоречил бирже
STATUS_CORRECTED = "corrected"  # сверено, но журнал держал другие числа — исправлены (это diff)
STATUS_UNMATCHED = "unmatched"  # филлы выхода не покрывают qty: их нет или есть лишние
STATUS_ENTRY_UNKNOWN = "entry_unknown"  # выход сошелся, вход подтвердить нечем
STATUS_ERROR = "error"  # биржа не ответила / сбой разбора
MATCHED = frozenset({STATUS_OK, STATUS_CORRECTED})

_QUERY_RE = re.compile(r"\?\S*")  # query подписанного URL ccxt — не в журнал и не в Telegram


class FillsSource(Protocol):
    """История исполнений: ccxt-биржа (демон) или read-only view (CLI); фейк в тестах."""

    def fetch_my_trades(
        self, symbol: str, since: int | None = None, limit: int | None = None,
        params: dict | None = None,
    ) -> list[dict[str, Any]]: ...


# ------------------------------------------------------------------ helpers


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_iso(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    # aware -> конвертацией в UTC (Codex #29), naive -> считаем UTC (все писатели пишут Z)
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _as_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _num(*values: Any) -> float | None:
    for v in values:
        if v is None or v == "" or isinstance(v, bool):
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _short(e: Exception) -> str:
    text = _QUERY_RE.sub("?<redacted>", str(e).replace("\n", " "))
    return f"{type(e).__name__}: {text}"[:160]


# -------------------------------------------------------------------- fills


@dataclass(frozen=True)
class Fill:
    key: tuple  # идентичность филла для дедупа (tradeId у BingX swap может не быть)
    order_id: str
    client_id: str  # нижний регистр: BingX может вернуть clientOrderID lowercase
    side: str  # buy | sell | "" (неизвестно)
    position_side: str  # long | short | "" (неизвестно)
    price: float
    amount: float  # в монетах
    fee: float | None  # quote, уплачено > 0; None = биржа не отдала
    ts: int | None  # мс

    def brief(self, role: str) -> dict[str, Any]:
        return {
            "role": role, "order": self.order_id, "side": self.side, "pos": self.position_side,
            "px": self.price, "amt": self.amount, "fee": self.fee, "ts": self.ts,
        }


_TS_KEYS = ("filledTm", "filledTime", "tradeTime", "time", "updateTime")


def _fill_ts(raw: dict, info: dict) -> int | None:
    ts = raw.get("timestamp")
    if isinstance(ts, int | float) and not isinstance(ts, bool) and ts > 0:
        return int(ts)
    # ccxt 4.5 читает 'filledTm', а пример ответа BingX в его же исходнике — 'filledTime':
    # без времени филл не попал бы ни в одно окно — разбираем сами
    for k in _TS_KEYS:
        v = info.get(k)
        if v is None or v == "":
            continue
        if isinstance(v, int | float) or str(v).isdigit():
            return int(float(v))
        dt = _parse_iso(v)
        if dt is not None:
            return _ms(dt)
    return None


def parse_fill(raw: Any) -> Fill | None:
    """ccxt trade-структура (fetch_my_trades) -> Fill; мусор -> None."""
    if not isinstance(raw, dict):
        return None
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    price = _num(raw.get("price"), info.get("price"), info.get("tradePrice"))
    amount = _num(raw.get("amount"), info.get("volume"), info.get("qty"))
    if price is None or amount is None or price <= 0 or amount <= 0:
        return None
    fee_obj = raw.get("fee")
    fee = _num(fee_obj.get("cost")) if isinstance(fee_obj, dict) else None
    if fee is None:
        commission = _num(info.get("commission"))
        fee = abs(commission) if commission is not None else None
    position_side = str(info.get("positionSide") or raw.get("positionSide") or "").lower()
    if position_side not in ("long", "short"):
        position_side = ""  # BOTH / пусто: сторону позиции подтвердить нечем
    order_id = str(raw.get("order") or info.get("orderId") or "")
    client_id = str(
        info.get("clientOrderId") or info.get("clientOrderID") or raw.get("clientOrderId") or ""
    ).lower()
    ts = _fill_ts(raw, info)
    trade_id = str(raw.get("id") or info.get("tradeId") or "")
    return Fill(
        key=(trade_id, order_id, ts, price, amount),
        order_id=order_id,
        client_id=client_id,
        side=str(raw.get("side") or info.get("side") or "").lower(),
        position_side=position_side,
        price=price,
        amount=amount,
        fee=fee,
        ts=ts,
    )


@dataclass(frozen=True)
class Ownership:
    """ТОЧНАЯ принадлежность филла сделке (ст. 1 п. 2): по id ордеров и clientOrderId."""

    exit_orders: frozenset[str]
    entry_orders: frozenset[str]
    client_id: str  # нижний регистр

    @classmethod
    def of(cls, t: sqlite3.Row) -> "Ownership":
        # entry_order_id добавляется в схему параллельно — колонки может не быть
        entry = t["entry_order_id"] if "entry_order_id" in t.keys() else None
        return cls(
            exit_orders=frozenset(str(x) for x in (t["sl_order_id"], t["tp_order_id"]) if x),
            entry_orders=frozenset({str(entry)} if entry else ()),
            client_id=str(t["client_order_id"] or "").lower(),
        )

    def owns(self, f: Fill) -> str | None:
        """'entry' | 'exit' | None (точно не опознан)."""
        if f.order_id and f.order_id in self.exit_orders:
            return "exit"
        if f.order_id and f.order_id in self.entry_orders:
            return "entry"
        if self.client_id and f.client_id:
            if f.client_id == self.client_id:
                return "entry"
            if f.client_id.startswith(self.client_id + "-"):  # -sl, -tp, -sl-r1 ...
                return "exit"
        return None


def _qty(fills: list[Fill]) -> float:
    return sum(f.amount for f in fills)


def _vwap(fills: list[Fill]) -> float:
    return sum(f.price * f.amount for f in fills) / _qty(fills)


def _fees(fills: list[Fill]) -> float:
    return sum(f.fee or 0.0 for f in fills)


def _fees_known(fills: list[Fill]) -> bool:
    return all(f.fee is not None for f in fills)


def _covers(filled: float, qty: float) -> bool:
    return qty > 0 and abs(filled - qty) <= QTY_TOLERANCE * qty


def _unique(fills: list[Fill]) -> list[Fill]:
    seen: set[tuple] = set()
    out = []
    for f in fills:
        if f.key not in seen:
            seen.add(f.key)
            out.append(f)
    return out


# ------------------------------------------------------------------- result


@dataclass
class TradeResult:
    trade_id: int
    decision_id: str
    symbol: str
    side: str
    close_reason: str | None
    closed_at: str | None
    status: str
    method: str = ""  # order_id (точно) | window (по стороне и времени)
    qty: float | None = None
    exit_qty: float | None = None
    exit_price: float | None = None
    exit_fees: float | None = None
    entry_price: float | None = None
    entry_fees: float | None = None
    entry_source: str = ""  # exchange | journal | exchange_window
    realized_pnl: float | None = None
    diffs: dict[str, list[float]] = field(default_factory=dict)  # поле -> [журнал, биржа]
    fills: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    @property
    def matched(self) -> bool:
        return self.status in MATCHED


@dataclass
class ReconcileReport:
    at: str
    run_id: str
    results: list[TradeResult] = field(default_factory=list)
    snapshot_id: int | None = None

    @property
    def clean(self) -> bool:
        """Все сверено, и журнал ни в чем не противоречил бирже."""
        return all(r.status == STATUS_OK for r in self.results)

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in (STATUS_OK, STATUS_CORRECTED, STATUS_UNMATCHED,
                              STATUS_ENTRY_UNKNOWN, STATUS_ERROR)}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out

    def payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "at": self.at,
            "checked": len(self.results),
            "counts": self.counts(),
            "trades": [asdict(r) for r in self.results],
        }


# --------------------------------------------------------------- reconcile


def _neighbours(t: sqlite3.Row, trades: list[sqlite3.Row]) -> tuple[datetime | None, datetime | None]:
    """closed_at предыдущей и created_at следующей сделки того же символа и стороны.

    Две сделки одного ключа одновременно не живут (executor отказывает в дубле), так
    что закрывающие филлы до первого и после второго момента — чужие."""
    prev_closed = next_created = None
    for u in trades:
        if u["id"] == t["id"] or u["symbol"] != t["symbol"] or u["side"] != t["side"]:
            continue
        if u["id"] < t["id"]:
            c = _parse_iso(u["closed_at"])
            if c is not None and (prev_closed is None or c > prev_closed):
                prev_closed = c
        else:
            c = _parse_iso(u["created_at"])
            if c is not None and (next_created is None or c < next_created):
                next_created = c
    return prev_closed, next_created


def _entry(
    t: sqlite3.Row, qty: float, exact: list[Fill], window_fn: Callable[[], list[Fill]],
) -> tuple[float, float, str, list[Fill]] | None:
    """Вход по приоритету: точные филлы > журнал (цена И комиссия) > окно открывающей стороны."""
    if _covers(_qty(exact), qty) and _fees_known(exact):
        return _vwap(exact), _fees(exact), "exchange", exact
    j_px, j_fee = _num(t["entry_price"]), _num(t["entry_fees"])
    if j_px is not None and j_fee is not None:
        return j_px, j_fee, "journal", []
    cand = _unique(exact + window_fn())
    if _covers(_qty(cand), qty) and _fees_known(cand):
        return _vwap(cand), _fees(cand), "exchange_window", cand
    return None


def _reconcile_one(
    exchange: FillsSource, t: sqlite3.Row, trades: list[sqlite3.Row], claimed: set[tuple], now: datetime,
) -> TradeResult:
    res = TradeResult(
        trade_id=int(t["id"]), decision_id=t["decision_id"], symbol=t["symbol"], side=t["side"],
        close_reason=t["close_reason"], closed_at=t["closed_at"], status=STATUS_UNMATCHED,
        qty=_num(t["qty"]),
    )
    qty = res.qty or 0.0
    if qty <= 0:
        res.note = "journal has no qty"
        return res

    created = _parse_iso(t["created_at"]) or now
    opened = _parse_iso(t["opened_at"]) or created
    closed = _parse_iso(t["closed_at"]) or now
    start = min(created, opened) - ENTRY_SKEW
    end = min(closed + EXIT_GRACE, now)
    prev_closed, next_created = _neighbours(t, trades)
    if prev_closed is not None and prev_closed > start:
        start = prev_closed
    if next_created is not None and next_created < end:
        end = next_created
    since = max(start, now - HISTORY_LIMIT)
    if end <= since:
        res.note = "empty search window (trade older than exchange history?)"
        return res

    raw = exchange.fetch_my_trades(
        t["symbol"], since=_ms(since), params={**FETCH_PARAMS, "until": _ms(end)}
    )
    fills = _unique([f for f in (parse_fill(x) for x in raw or []) if f is not None and f.key not in claimed])
    mine = Ownership.of(t)
    others = [Ownership.of(u) for u in trades if u["id"] != t["id"] and u["symbol"] == t["symbol"]]
    close_side, open_side = ("sell", "buy") if t["side"] == "long" else ("buy", "sell")

    def exact(role: str, side: str) -> list[Fill]:
        # id точный, но сторону все равно сверяем: старые обрезанные clientOrderId могли
        # совпасть у long и short одного символа (Claudexor #19, ревью 2026-10-13)
        return [
            f for f in fills
            if mine.owns(f) == role and f.side in ("", side) and f.position_side in ("", t["side"])
        ]

    def window(side: str, lo: datetime, hi: datetime) -> list[Fill]:
        lo_ms, hi_ms = _ms(lo), _ms(hi)
        return [
            f for f in fills
            if mine.owns(f) is None
            and not any(o.owns(f) for o in others)
            and f.side == side
            and f.position_side in ("", t["side"])
            and f.ts is not None and lo_ms <= f.ts <= hi_ms
        ]

    exact_exit = exact("exit", close_side)
    exit_fills, res.method = exact_exit, "order_id"
    if not _covers(_qty(exact_exit), qty):
        exit_fills, res.method = _unique(exact_exit + window(close_side, start, end)), "window"
    res.exit_qty = _qty(exit_fills)
    res.fills = [f.brief("exit") for f in exit_fills]
    if not _covers(res.exit_qty, qty):
        res.note = f"exit fills cover {res.exit_qty:.8g} of qty {qty:.8g}"
        return res
    if not _fees_known(exit_fills):
        res.note = "exit fill without fee"
        return res

    exact_entry = exact("entry", open_side)
    entry = _entry(t, qty, exact_entry, lambda: window(open_side, start, min(opened + ENTRY_SKEW, end)))
    if entry is None:
        res.status = STATUS_ENTRY_UNKNOWN
        res.note = "entry not verifiable: no entry fills and journal entry price/fee incomplete"
        return res
    entry_px, entry_fee, res.entry_source, entry_fills = entry
    res.fills += [f.brief("entry") for f in entry_fills]

    exit_px, exit_fee = _vwap(exit_fills), _fees(exit_fills)
    sign = 1.0 if t["side"] == "long" else -1.0
    pnl = sign * (exit_px - entry_px) * res.exit_qty - entry_fee - exit_fee
    res.entry_price, res.entry_fees = entry_px, entry_fee
    res.exit_price, res.exit_fees, res.realized_pnl = exit_px, exit_fee, pnl
    for name, new, (rel, abs_) in (
        ("entry_price", entry_px, _PRICE_TOL), ("entry_fees", entry_fee, _FEE_TOL),
        ("exit_price", exit_px, _PRICE_TOL), ("exit_fees", exit_fee, _FEE_TOL),
        ("realized_pnl", pnl, _PNL_TOL),
    ):
        old = _num(t[name])
        if old is not None and abs(old - new) > max(abs_, rel * abs(new)):
            res.diffs[name] = [old, new]
    res.status = STATUS_CORRECTED if res.diffs else STATUS_OK
    claimed.update(f.key for f in exit_fills + entry_fills)
    return res


def reconcile_closed_trades(
    exchange: FillsSource,
    journal: sqlite3.Connection,
    now: datetime,
    *,
    run_id: str = "reconcile",
    write: bool = True,
) -> ReconcileReport:
    """Сверить все closed-сделки с reconciled=0 по филлам биржи и записать результат.

    write=False — только отчет (CLI --dry-run): журнал не трогается."""
    now = _as_utc(now)
    trades = journal.execute("SELECT * FROM trades ORDER BY id").fetchall()
    report = ReconcileReport(at=_iso(now), run_id=run_id)
    claimed: set[tuple] = set()
    for t in trades:
        if t["status"] != "closed" or t["reconciled"]:
            continue
        try:
            r = _reconcile_one(exchange, t, trades, claimed, now)
        except Exception as e:  # noqa: BLE001 - одна сделка не валит прогон
            logger.warning("reconcile trade %s failed: %s", t["id"], _short(e))
            r = TradeResult(
                trade_id=int(t["id"]), decision_id=t["decision_id"], symbol=t["symbol"],
                side=t["side"], close_reason=t["close_reason"], closed_at=t["closed_at"],
                status=STATUS_ERROR, qty=_num(t["qty"]), note=_short(e),
            )
        report.results.append(r)
    if write:
        _write(journal, report)
    c = report.counts()
    logger.info(
        "reconcile: %d checked, %d ok, %d corrected, %d unmatched, %d entry_unknown, %d errors",
        len(report.results), c[STATUS_OK], c[STATUS_CORRECTED], c[STATUS_UNMATCHED],
        c[STATUS_ENTRY_UNKNOWN], c[STATUS_ERROR],
    )
    return report


def _write(journal: sqlite3.Connection, report: ReconcileReport) -> None:
    with transaction(journal):
        for r in report.results:
            if not r.matched:
                continue  # несверенное не трогаем: reconciled=0, строка — в снепшоте
            journal.execute(
                "UPDATE trades SET entry_price=?, entry_fees=?, exit_price=?, exit_fees=?, "
                "realized_pnl=?, reconciled=1 WHERE id=? AND reconciled=0",
                (r.entry_price, r.entry_fees, r.exit_price, r.exit_fees, r.realized_pnl, r.trade_id),
            )
        cur = journal.execute(
            "INSERT INTO sync_snapshots (run_id, kind, payload_json, clean, created_at) "
            "VALUES (?, 'reconcile', ?, ?, ?)",
            (report.run_id, json.dumps(report.payload()), int(report.clean), report.at),
        )
        report.snapshot_id = cur.lastrowid


def render_reconcile(report: ReconcileReport) -> str:
    """ASCII-отчет для CLI (cp1251-консоль Windows, урок v1)."""
    if not report.results:
        lines = ["RECONCILE CLEAN: no unreconciled closed trades"]
    elif report.clean:
        lines = [f"RECONCILE CLEAN: {len(report.results)} closed trade(s) match exchange fills"]
    else:
        lines = ["RECONCILE DIRTY: " + ", ".join(f"{k}={v}" for k, v in report.counts().items() if v)]
    for r in report.results:
        line = f"  [{r.status}] #{r.trade_id} {r.symbol} {r.side} {r.close_reason or '-'}"
        if r.matched:
            line += (
                f": exit {r.exit_price:.8g} x {r.exit_qty:.8g}, entry {r.entry_price:.8g} "
                f"({r.entry_source}), fees {(r.entry_fees or 0.0) + (r.exit_fees or 0.0):.6g}, "
                f"pnl {r.realized_pnl:+.4f} [{r.method}]"
            )
            for k, (old, new) in r.diffs.items():
                line += f"; journal {k} {old:.8g} -> {new:.8g}"
        else:
            line += f": {r.note}"
        lines.append(line)
    if report.snapshot_id is not None:
        lines.append(f"sync_snapshots #{report.snapshot_id} written (clean={int(report.clean)})")
    return "\n".join(lines).encode("ascii", "replace").decode("ascii")


def cli_reconcile(
    view: FillsSource, db_path: Path, *, write: bool = True, now: datetime | None = None
) -> int:
    """`oskar sync reconcile`: 0 — чисто, 1 — есть несверенное или расхождения."""
    from oskar.journal.db import connect

    conn = connect(db_path)
    try:
        report = reconcile_closed_trades(view, conn, now or datetime.now(UTC), run_id="cli", write=write)
    finally:
        conn.close()
    print(render_reconcile(report))
    return 0 if report.clean else 1


# ---------------------------------------------------------------- schedule


class ClosedTradeReconciler:
    """Расписание сверки (ст. 1 п. 1): сразу после закрытой несверенной сделки, далее раз в 6 ч.

    tick(now) зовется из цикла монитора позиций и почти всегда стоит один SELECT. Прогон:
    - есть closed-сделка с reconciled=0, которую еще не пробовали, или пробовали > 10 мин
      назад и попыток меньше fast_retries -> сразу;
    - иначе каждые interval_sec (первый — через interval_sec после первого тика).
    Застрявшая сделка после fast_retries повторяется только 6-часовым прогоном и один раз
    уходит в Telegram — не снепшот и не алерт каждые 10 минут.
    """

    def __init__(
        self,
        exchange: FillsSource,
        journal: sqlite3.Connection,
        *,
        interval_sec: float = DEFAULT_INTERVAL_SEC,
        retry_sec: float = RETRY_SEC,
        fast_retries: int = FAST_RETRIES,
        run_id: str = "reconcile",
        notify: Callable[[str], Any] | None = None,
    ):
        self._ex = exchange
        self._j = journal
        self._interval = timedelta(seconds=interval_sec)
        self._retry = timedelta(seconds=retry_sec)
        self._fast_retries = fast_retries
        self._run_id = run_id
        self._notify = notify
        self._attempts: dict[int, tuple[int, datetime]] = {}  # trade id -> (попыток, последняя)
        self._notified: set[int] = set()
        self._next_periodic: datetime | None = None
        self.last_report: ReconcileReport | None = None

    def pending_ids(self) -> list[int]:
        rows = self._j.execute(
            "SELECT id FROM trades WHERE status='closed' AND reconciled=0 ORDER BY id"
        ).fetchall()
        return [int(r["id"]) for r in rows]

    def due(self, now: datetime) -> bool:
        now = _as_utc(now)
        for tid in self.pending_ids():
            if tid not in self._attempts:
                return True  # новая закрытая сделка — сверка сразу
            tries, last = self._attempts[tid]
            if tries < self._fast_retries and now - last >= self._retry:
                return True
        if self._next_periodic is None:
            self._next_periodic = now + self._interval
            return False
        return now >= self._next_periodic

    def tick(self, now: datetime) -> ReconcileReport | None:
        if not self.due(now):
            return None
        return self.run(now)

    def run(self, now: datetime) -> ReconcileReport:
        now = _as_utc(now)
        # попытка и следующий период фиксируются ДО прогона: упавший прогон (журнал, биржа)
        # не превращается в опрос биржи на каждом тике монитора
        self._next_periodic = now + self._interval
        for tid in self.pending_ids():
            tries = self._attempts[tid][0] if tid in self._attempts else 0
            self._attempts[tid] = (tries + 1, now)
        report = reconcile_closed_trades(self._ex, self._j, now, run_id=self._run_id)
        for r in report.results:
            if r.matched:
                self._attempts.pop(r.trade_id, None)
            elif r.trade_id not in self._attempts:  # закрылась между SELECT'ами
                self._attempts[r.trade_id] = (1, now)
        self._alert(report)
        self.last_report = report
        return report

    def _alert(self, report: ReconcileReport) -> None:
        for r in report.results:
            if r.trade_id in self._notified:
                continue
            if r.status == STATUS_CORRECTED:
                changes = ", ".join(f"{k} {old:.6g} → {new:.6g}" for k, (old, new) in r.diffs.items())
                text = (f"⚠️ reconcile: журнал расходился с биржей — {r.symbol} {r.side} "
                        f"#{r.trade_id}: {changes} (исправлено по филлам)")
            elif not r.matched and self._attempts.get(r.trade_id, (0,))[0] >= self._fast_retries:
                text = (f"⚠️ reconcile: {r.symbol} {r.side} #{r.trade_id} не сверяется с историей "
                        f"биржи ({r.status}: {r.note}) — проверь руками")
            else:
                continue
            self._notified.add(r.trade_id)
            logger.warning(text)
            if self._notify is not None:
                try:
                    self._notify(html.escape(text, quote=False))  # send_text шлет parse_mode=HTML
                except Exception as e:  # noqa: BLE001 - канал не влияет на учет
                    logger.warning("reconcile notify failed: %s", e)
