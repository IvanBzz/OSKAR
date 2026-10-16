"""Вердикты инвариантов (CONSTITUTION.md ст. 5) — машинная проверка по журналу.

Ревью 2026-10-13 (Codex #27, Claudexor a04): config/invariants.yaml объявлял INV-001..003,
а ops/invariants.py не существовало — статья 5 оставалась декларацией. Здесь каждая
метрика, названная в invariants.yaml, — функция над журналом, а вердикт считается по окну:

- {closed_trades: N} — последние N закрытых сделок; {hours: N} — закрытые за N часов;
- green — ожидание выполнено на ПОЛНОМ окне, данные однозначны;
- red — ожидание нарушено. Для верхних границ (== 0, <=, <) — даже на неполном окне:
  больше данных нарушение не отменит;
- insufficient_data — сделок в окне меньше N, P&L/длительность закрытия неизвестны,
  сделки не прошли сверку с биржей. Это НЕ green: ст. 5 п. 2 снимает
  committed_not_live_validated только зеленым вердиктом, и «данных нет» не должно
  читаться как «все хорошо»;
- error — метрика не реализована, окно/ожидание не разобраны, сбой расчета: инвариант,
  который нельзя проверить машиной, проверку не прошел (ст. 5 п. 1).

Каждый прогон пишет заметку-вердикт в vault/runs/ (имя — вывод, ст. 7 п. 3).
НЕ реализовано (следующий шаг ст. 5): ledger committed_not_live_validated, гейт «не более
2 невалидированных изменений», red -> Telegram-алерт / авто-revert.
"""

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from oskar.sync.reconcile import STATUS_OK

REPO_ROOT = Path(__file__).resolve().parents[3]
INVARIANTS_PATH = REPO_ROOT / "config" / "invariants.yaml"
RUNS_DIR = REPO_ROOT / "vault" / "runs"

GREEN, RED, INSUFFICIENT, ERROR = "green", "red", "insufficient_data", "error"

# CHECK-список close_reason в journal/db.py; все прочее (NULL, 'unknown' из v1) — причина неизвестна
KNOWN_CLOSE_REASONS = frozenset(
    {"sl_hit", "tp_hit", "trailing_or_partial", "duration_timeout", "panic", "manual"}
)
# Duration-stop монитора срабатывает на первом тике после max_loss_hold (тик 60 с) и закрывает
# по рынку с подтверждением по позициям: 5 минут — люфт исполнения, а не нарушение инварианта.
OVERHOLD_GRACE_SEC = 300
# INV-004 (блок H, 2026-10-16): исполнитель проверяет R:R и стоп по цене, которую видит перед
# отправкой; филл маркета может уйти на проскальзывание. Люфт — чтобы инвариант ловил обход
# капа (BERA: R:R 0.23 у кнопки «Маркет»), а не тик проскальзывания.
FILL_RR_ALLOWANCE = 0.1
FILL_SL_PCT_ALLOWANCE = 0.05
MIN_TRADES_IN_HOURS_WINDOW = 1  # окно по времени без единой закрытой сделки — не доказательство
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_EXPECT_RE = re.compile(r"^\s*(==|!=|<=|>=|<|>)\s*(-?\d+(?:\.\d+)?)\s*$")


# ------------------------------------------------------------------- model


@dataclass(frozen=True)
class Window:
    closed_trades: int | None = None
    hours: float | None = None

    @classmethod
    def parse(cls, raw: Any) -> "Window":
        if not isinstance(raw, dict) or len(raw) != 1:
            raise ValueError(f"window must be {{closed_trades: N}} or {{hours: N}}, got {raw!r}")
        ((key, value),) = raw.items()
        if key == "closed_trades" and int(value) > 0:
            return cls(closed_trades=int(value))
        if key == "hours" and float(value) > 0:
            return cls(hours=float(value))
        raise ValueError(f"bad window {raw!r}")


@dataclass(frozen=True)
class Measurement:
    value: float | None
    n: int  # объем доказательств: закрытых / сверенных сделок в окне
    sufficient: bool  # окно полное и данные однозначны
    detail: str


@dataclass(frozen=True)
class Verdict:
    id: str
    metric: str
    status: str  # green | red | insufficient_data | error
    value: float | None
    n: int
    expect: str
    window: str
    detail: str
    description: str = ""


@dataclass
class Context:
    """Параметры метрик, которые читаются из конфигов лениво (тесты подставляют свои)."""

    max_loss_hold_seconds: int | None = None
    caps: Any = None  # risk.ladder.Caps
    profile_limits: Any = None  # risk.ladder.ProfileLimits

    def entry_limits(self) -> tuple[Any, Any]:
        if self.caps is None or self.profile_limits is None:
            from oskar.core.bootstrap import load_active_profile, load_caps

            self.caps = self.caps or load_caps()
            self.profile_limits = self.profile_limits or load_active_profile().limits
        return self.caps, self.profile_limits

    def loss_hold_limit(self) -> int:
        if self.max_loss_hold_seconds is None:
            from oskar.core.bootstrap import load_active_profile

            self.max_loss_hold_seconds = load_active_profile().max_loss_hold_seconds
        return self.max_loss_hold_seconds


MetricFn = Callable[[sqlite3.Connection, Window, datetime, Context], Measurement]


# ------------------------------------------------------------------ window


def _parse_iso(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _closed_at(t: sqlite3.Row) -> datetime | None:
    return _parse_iso(t["closed_at"])


def closed_trades_in(
    journal: sqlite3.Connection, window: Window, now: datetime
) -> tuple[list[sqlite3.Row], bool]:
    """Закрытые сделки окна (новые первыми) и признак «окно полное»."""
    rows = [
        r for r in journal.execute("SELECT * FROM trades WHERE status = 'closed'").fetchall()
        if (_closed_at(r) or _EPOCH) <= now
    ]
    rows.sort(key=lambda r: (_closed_at(r) or _EPOCH, r["id"]), reverse=True)
    if window.closed_trades is not None:
        picked = rows[: window.closed_trades]
        return picked, len(picked) >= window.closed_trades
    cutoff = now - timedelta(hours=float(window.hours or 0))
    picked = [r for r in rows if (c := _closed_at(r)) is not None and c >= cutoff]
    return picked, len(picked) >= MIN_TRADES_IN_HOURS_WINDOW


def _shortfall(window: Window, trades: list[sqlite3.Row]) -> str:
    if window.closed_trades is not None and len(trades) < window.closed_trades:
        return f"; в окне {len(trades)} из {window.closed_trades} закрытых сделок"
    if window.hours is not None and len(trades) < MIN_TRADES_IN_HOURS_WINDOW:
        return f"; за {window.hours:g} ч нет закрытых сделок"
    return ""


def _ids(trades: list[sqlite3.Row], limit: int = 10) -> str:
    head = ", ".join(f"#{t['id']}" for t in trades[:limit])
    return head + (f" и еще {len(trades) - limit}" if len(trades) > limit else "")


# ----------------------------------------------------------------- metrics


def close_reason_unknown_count(
    journal: sqlite3.Connection, window: Window, now: datetime, ctx: Context
) -> Measurement:
    """INV-001: закрытия без известной причины (v1: 76% unknown/manual)."""
    trades, full = closed_trades_in(journal, window, now)
    bad = [t for t in trades if (t["close_reason"] or "") not in KNOWN_CLOSE_REASONS]
    detail = f"{len(bad)} из {len(trades)} закрытий без известной причины"
    if bad:
        detail += f" ({_ids(bad)})"
    return Measurement(len(bad), len(trades), full, detail + _shortfall(window, trades))


def _duration_sec(t: sqlite3.Row) -> float | None:
    if t["duration_seconds"] is not None:
        return float(t["duration_seconds"])
    opened, closed = _parse_iso(t["opened_at"]), _closed_at(t)
    if opened is None or closed is None:
        return None
    return (closed - opened).total_seconds()


def loser_overhold_count(
    journal: sqlite3.Connection, window: Window, now: datetime, ctx: Context
) -> Measurement:
    """INV-002: лосеры, прожившие дольше max_loss_hold_seconds активного профиля (+ люфт).

    Сделка дольше лимита с неизвестным P&L (manual/panic до сверки) или без длительности
    не классифицируется — окно не доказывает green, пока сверка ее не разрешит."""
    limit = ctx.loss_hold_limit()
    trades, full = closed_trades_in(journal, window, now)
    over, unknown = [], []
    for t in trades:
        dur = _duration_sec(t)
        if dur is not None and dur <= limit + OVERHOLD_GRACE_SEC:
            continue  # прожила меньше лимита — лосер она или нет, неважно
        pnl = t["realized_pnl"]
        # P&L без сверки по филлам биржи — оценка монитора, не факт: зеленым по нему
        # INV-002 быть не может (перепроверка Codex: 20 несверенных «прибыльных» — green)
        reconciled = ("reconciled" in t.keys()) and bool(t["reconciled"])
        if dur is None or pnl is None or not reconciled:
            unknown.append(t)
        elif pnl < 0:
            over.append(t)
    detail = (f"{len(over)} лосеров дольше {limit} с (+{OVERHOLD_GRACE_SEC} с люфта) "
              f"из {len(trades)} закрытых")
    if over:
        detail += f" ({_ids(over)})"
    if unknown:
        detail += f"; без сверенного P&L или длительности, не классифицировать: {_ids(unknown)}"
    return Measurement(len(over), len(trades), full and not unknown, detail + _shortfall(window, trades))


def reconcile_diff_rows(
    journal: sqlite3.Connection, window: Window, now: datetime, ctx: Context
) -> Measurement:
    """INV-003: расхождения журнала с биржей по последним снепшотам сверки (kind='reconcile').

    По каждой закрытой сделке окна берется ее ПОСЛЕДНИЙ результат в sync_snapshots: любой
    статус кроме 'reconciled' (журнал врал и исправлен / филлы не покрыли qty / вход не
    подтвердить / сбой) — строка расхождения. Сделка без результата (сверка ее не видела,
    или монитор сам поставил reconciled=1) — не доказательство: окно неполное."""
    trades, full = closed_trades_in(journal, window, now)
    if not trades:
        return Measurement(0, 0, False, "в окне нет закрытых сделок — сверять нечего"
                           + _shortfall(window, trades))
    since = min((_closed_at(t) or _EPOCH) for t in trades)
    rows = journal.execute(
        "SELECT created_at, payload_json FROM sync_snapshots "
        "WHERE kind = 'reconcile' AND created_at >= ? ORDER BY created_at, id",
        (since.strftime("%Y-%m-%dT%H:%M:%S"),),
    ).fetchall()
    latest: dict[int, dict] = {}
    for row in rows:
        created = _parse_iso(row["created_at"])
        if created is None or created > now:
            continue
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue  # битый снепшот не доказывает ничего
        for r in payload.get("trades") or []:
            if isinstance(r, dict) and r.get("trade_id") is not None:
                latest[int(r["trade_id"])] = r
    diffs, unchecked = [], []
    for t in trades:
        r = latest.get(int(t["id"]))
        if r is None:
            unchecked.append(t)
        elif r.get("status") != STATUS_OK:
            diffs.append((t, r))
    checked = len(trades) - len(unchecked)
    detail = f"{len(diffs)} расхождений по {checked} сверенным сделкам из {len(trades)}"
    if diffs:
        detail += " (" + ", ".join(f"#{t['id']} {r.get('status')}" for t, r in diffs[:10]) + ")"
    if unchecked:
        detail += f"; не сверены с историей биржи: {_ids(unchecked)}"
    return Measurement(len(diffs), checked, full and not unchecked, detail + _shortfall(window, trades))


def entry_price_violations(
    journal: sqlite3.Connection, window: Window, now: datetime, ctx: Context
) -> Measurement:
    """INV-004: закрытые сделки, чей вход по цене ФИЛЛА нарушал кап R:R или диапазон стопа профиля.

    Блок H (2026-10-16): лестница проверяла цену плана, а кнопка «Маркет» и дрейф до 0.5%
    входили по другой (BERA: R:R 0.23 при капе 2.0). Пересчет — той же check_entry_price,
    что у лестницы и исполнителя, с люфтом на проскальзывание филла."""
    from oskar.risk.sizing import fee_adjusted_rr

    caps, limits = ctx.entry_limits()
    trades, full = closed_trades_in(journal, window, now)
    bad, unknown = [], []
    for t in trades:
        e, sl, tp = t["entry_price"], t["sl_price"], t["tp_price"]
        if not e or not sl or not tp:
            unknown.append(t)
            continue
        sl_pct = abs(e - sl) / e * 100.0
        rr = fee_adjusted_rr(e, sl, tp)
        if (rr < caps.min_rr_fee_adjusted - FILL_RR_ALLOWANCE
                or sl_pct < limits.sl_dist_pct_min - FILL_SL_PCT_ALLOWANCE
                or sl_pct > limits.sl_dist_pct_max + FILL_SL_PCT_ALLOWANCE):
            bad.append(t)
    detail = (f"{len(bad)} входов вне капа R:R >= {caps.min_rr_fee_adjusted} "
              f"или стопа [{limits.sl_dist_pct_min}..{limits.sl_dist_pct_max}]% "
              f"по цене филла из {len(trades)} закрытых")
    if bad:
        detail += f" ({_ids(bad)})"
    if unknown:
        detail += f"; без entry/SL/TP, не классифицировать: {_ids(unknown)}"
    return Measurement(len(bad), len(trades), full and not unknown, detail + _shortfall(window, trades))


def cf_v2_coverage_violations(
    journal: sqlite3.Connection, window: Window, now: datetime, ctx: Context
) -> Measurement:
    """INV-006 (блок R3, 2026-10-16): CF резолвера v2 с исходом имеют полное покрытие окна.

    v1 превращал отсутствие свечей в expired_flat. v2 пишет исход только при барах подряд от
    начала окна до бара исхода и доказательство в outcome_detail (start_ms, last_bar_ms, bars,
    tf_ms); метрика перепроверяет его по журналу. no_data — честное «не измерено», не в счет.
    Окно: {hours: N} — разрешенные за N часов; {closed_trades: N} — последние N разрешенных."""
    from oskar.core.cf_resolver import CF_RESOLVER_VERSION, COVERED_OUTCOMES, coverage_problem

    cols = {r[1] for r in journal.execute("PRAGMA table_info(counterfactuals)")}
    if "resolver_version" not in cols:
        return Measurement(None, 0, False, "журнал до v5: колонок резолвера v2 нет")
    marks = ",".join("?" * len(COVERED_OUTCOMES))
    rows = [
        r for r in journal.execute(
            "SELECT id, hypothetical_outcome, outcome_detail, realized_r, resolved_at "
            f"FROM counterfactuals WHERE resolver_version = ? AND hypothetical_outcome IN ({marks})",
            (CF_RESOLVER_VERSION, *COVERED_OUTCOMES),
        ).fetchall()
        if (_parse_iso(r["resolved_at"]) or _EPOCH) <= now
    ]
    rows.sort(key=lambda r: (_parse_iso(r["resolved_at"]) or _EPOCH, r["id"]), reverse=True)
    if window.closed_trades is not None:
        picked = rows[: window.closed_trades]
        full = len(picked) >= window.closed_trades
    else:
        cutoff = now - timedelta(hours=float(window.hours or 0))
        picked = [r for r in rows if (_parse_iso(r["resolved_at"]) or _EPOCH) >= cutoff]
        full = len(picked) >= MIN_TRADES_IN_HOURS_WINDOW
    bad = []
    for r in picked:
        why = coverage_problem(r["outcome_detail"], r["hypothetical_outcome"])
        if why is None and r["realized_r"] is None:
            why = "нет realized_r"
        if why is not None:
            bad.append(f"#{r['id']} {r['hypothetical_outcome']}: {why}")
    detail = f"{len(bad)} из {len(picked)} исходов CF v2 без доказанного полного покрытия"
    if bad:
        detail += " (" + "; ".join(bad[:10]) + (f" и еще {len(bad) - 10}" if len(bad) > 10 else "") + ")"
    if not full:
        detail += "; в окне нет разрешенных CF v2" if not picked else "; окно неполное"
    return Measurement(len(bad), len(picked), full, detail)


METRICS: dict[str, MetricFn] = {
    "cf_v2_coverage_violations": cf_v2_coverage_violations,
    "entry_price_violations": entry_price_violations,
    "close_reason_unknown_count": close_reason_unknown_count,
    "loser_overhold_count": loser_overhold_count,
    "reconcile_diff_rows": reconcile_diff_rows,
}


# ----------------------------------------------------------------- verdict


def parse_expect(expect: str) -> tuple[str, float]:
    m = _EXPECT_RE.match(str(expect))
    if not m:
        raise ValueError(f"expect must be '<op> <number>' (==, !=, <=, >=, <, >), got {expect!r}")
    return m.group(1), float(m.group(2))


def _holds(op: str, target: float, value: float | None) -> bool:
    if value is None:
        return False
    return {
        "==": value == target, "!=": value != target, "<=": value <= target,
        ">=": value >= target, "<": value < target, ">": value > target,
    }[op]


def _violation_is_final(op: str, target: float, value: float | None) -> bool:
    """Верхняя граница на счетчик уже пробита: больше данных этого не отменит."""
    if value is None:
        return False
    return op in ("<", "<=") or (op == "==" and value > target)


def _window_label(raw: Any) -> str:
    if isinstance(raw, dict):
        return ", ".join(f"{k}={v}" for k, v in raw.items())
    return str(raw)


def load_invariants(path: Path = INVARIANTS_PATH) -> list[dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [x for x in (raw.get("invariants") or []) if isinstance(x, dict)]


def _verdict(journal: sqlite3.Connection, now: datetime, inv: dict, ctx: Context) -> Verdict:
    base = {
        "id": str(inv.get("id") or "?"),
        "metric": str(inv.get("metric") or ""),
        "expect": str(inv.get("expect") or ""),
        "window": _window_label(inv.get("window")),
        "description": str(inv.get("description") or ""),
    }
    try:
        window = Window.parse(inv.get("window"))
        op, target = parse_expect(base["expect"])
        fn = METRICS.get(base["metric"])
        if fn is None:
            raise LookupError(f"metric '{base['metric']}' is not implemented in ops/invariants.py")
        m = fn(journal, window, now, ctx)
    except Exception as e:  # noqa: BLE001 - непроверяемый инвариант = error, не green
        return Verdict(**base, status=ERROR, value=None, n=0, detail=f"{type(e).__name__}: {e}")
    ok = _holds(op, target, m.value)
    if m.sufficient:
        status = GREEN if ok else RED
    elif not ok and _violation_is_final(op, target, m.value):
        status = RED
    else:
        status = INSUFFICIENT
    return Verdict(**base, status=status, value=m.value, n=m.n, detail=m.detail)


def evaluate(
    journal: sqlite3.Connection,
    now: datetime,
    *,
    invariants: list[dict] | None = None,
    max_loss_hold_seconds: int | None = None,
) -> list[Verdict]:
    """Вердикт по каждому инварианту из config/invariants.yaml (или переданному списку)."""
    now = now.astimezone(UTC) if now.tzinfo is not None else now.replace(tzinfo=UTC)
    ctx = Context(max_loss_hold_seconds=max_loss_hold_seconds)
    invs = load_invariants() if invariants is None else invariants
    return [_verdict(journal, now, inv, ctx) for inv in invs]


def overall_status(verdicts: list[Verdict]) -> str:
    statuses = {v.status for v in verdicts}
    for s in (RED, ERROR, INSUFFICIENT):
        if s in statuses:
            return s
    return GREEN if verdicts else INSUFFICIENT


def exit_code(verdicts: list[Verdict]) -> int:
    """1 — есть red или error (CLI/CI), 0 — иначе (insufficient_data не провал прогона)."""
    return 1 if any(v.status in (RED, ERROR) for v in verdicts) else 0


# -------------------------------------------------------------------- note


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _id_list(ids: list[str]) -> str:
    return ", ".join(ids[:3]) + (f" и еще {len(ids) - 3}" if len(ids) > 3 else "")


def verdict_conclusion(verdicts: list[Verdict]) -> str:
    """Вывод для имени заметки (ст. 7 п. 3: заметка называется выводом, не темой)."""
    if not verdicts:
        return "инвариантов не объявлено"
    by = {s: [v.id for v in verdicts if v.status == s] for s in (GREEN, RED, INSUFFICIENT, ERROR)}
    if len(by[GREEN]) == len(verdicts):
        return f"все {len(verdicts)} зеленые"
    parts = []
    if by[RED]:
        parts.append(("красный " if len(by[RED]) == 1 else "красные ") + _id_list(by[RED]))
    if by[ERROR]:
        parts.append(("не проверен " if len(by[ERROR]) == 1 else "не проверены ") + _id_list(by[ERROR]))
    if by[INSUFFICIENT]:
        parts.append("без данных " + _id_list(by[INSUFFICIENT]))
    if by[GREEN]:
        parts.append(f"зеленых {len(by[GREEN])} из {len(verdicts)}")
    return ", ".join(parts)


def note_title(verdicts: list[Verdict], now: datetime) -> str:
    return f"Инварианты {now.astimezone(UTC):%Y-%m-%d %H-%M} UTC — {verdict_conclusion(verdicts)}"


def render_note(verdicts: list[Verdict], now: datetime) -> str:
    now = now.astimezone(UTC)

    def cell(s: Any) -> str:
        return str(s).replace("|", "\\|")

    rows = [
        f"| {cell(v.id)} | {cell(v.metric)} | {cell(v.window)} | {cell(v.expect)} | "
        f"{_fmt(v.value)} | {v.n} | **{v.status}** |"
        for v in verdicts
    ]
    details = [f"- **{v.id}** ({v.status}) — {v.description}: {v.detail}" for v in verdicts]
    return "\n".join([
        "---",
        "tags: [runs, invariants, validation]",
        f"date: {now:%Y-%m-%d}",
        f"status: {overall_status(verdicts)}",
        "---",
        f"# {note_title(verdicts, now)}",
        "",
        f"Прогон `oskar invariants` (ст. 5) по журналу, {now:%Y-%m-%d %H:%M} UTC. "
        "insufficient_data — не green: статус committed_not_live_validated снимает только green.",
        "",
        "| ID | Метрика | Окно | Ожидание | Значение | n | Вердикт |",
        "|---|---|---|---|---|---|---|",
        *rows,
        "",
        "## Детали",
        "",
        *details,
        "",
        "## Связи",
        "",
        "- [[конституция — 7 статей, капсы hash-pinned]]",
        "- [[changelog]]",
        "",
    ])


def write_verdict_note(verdicts: list[Verdict], now: datetime, runs_dir: Path = RUNS_DIR) -> Path:
    """Заметка-вердикт в vault/runs/; второй прогон в ту же минуту не затирает первый."""
    title = note_title(verdicts, now)
    stem = title[0].lower() + title[1:]
    runs_dir.mkdir(parents=True, exist_ok=True)
    path, n = runs_dir / f"{stem}.md", 2
    while path.exists():
        path, n = runs_dir / f"{stem} ({n}).md", n + 1
    path.write_text(render_note(verdicts, now), encoding="utf-8", newline="\n")
    return path


def render_verdicts(verdicts: list[Verdict]) -> str:
    lines = []
    for v in verdicts:
        lines.append(f"{v.id} {v.status.upper()}: {v.metric}={_fmt(v.value)} "
                     f"(expect {v.expect}; window {v.window}; n={v.n})")
        lines.append(f"    {v.detail}")
    lines.append(f"overall: {overall_status(verdicts)}")
    return "\n".join(lines)


def cli_main(
    db_path: Path,
    *,
    write_note: bool = True,
    now: datetime | None = None,
    runs_dir: Path = RUNS_DIR,
    invariants: list[dict] | None = None,
    max_loss_hold_seconds: int | None = None,
) -> int:
    """`oskar invariants`: вердикты в stdout + заметка в vault/runs/; exit 1 на red/error."""
    from oskar.journal.db import connect

    now = now or datetime.now(UTC)
    conn = connect(db_path)
    try:
        verdicts = evaluate(conn, now, invariants=invariants, max_loss_hold_seconds=max_loss_hold_seconds)
    finally:
        conn.close()
    print(render_verdicts(verdicts))
    if write_note:
        print(f"verdict note: {write_verdict_note(verdicts, now, runs_dir)}")
    return exit_code(verdicts)
