"""Дневная сводка OSKAR в Telegram (блок I, 2026-10-16) и срезы журнала для API.

Раз в сутки (settings.ops.digest_time, по умолчанию 21:00 Europe/Moscow) и по команде /digest
сигнального бота — 8-10 строк: воронка за 24 ч, сделки и P&L (сверенный по филлам биржи и
несверенный — отдельно, ст. 1 п. 1), вызовы LLM и остаток бюджета, аптайм и защита, вердикты
инвариантов. Все цифры — из журнала и состояния демона, ничего не выдумывается: нет данных —
так и пишем. Текст — HTML (parse_mode как у прочих сообщений), динамика экранирована.

Те же срезы отдают GET /positions и GET /pnl (скилл oskar-control).
"""

import html
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

WINDOW_HOURS = 24


def iso(dt: datetime) -> str:
    """Формат created_at/closed_at журнала: строковое сравнение = хронологическое."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ------------------------------------------------------------------ срезы


def funnel(journal: sqlite3.Connection, since: datetime, until: datetime) -> dict:
    """Воронка решений окна: кандидаты -> лестница -> Scout -> Trader -> финальная лестница
    -> исполнено (вход ушел на биржу = строка trades)."""
    row = journal.execute(
        "SELECT COUNT(*) AS candidates, "
        "COALESCE(SUM(json_extract(risk_json, '$.passed') = 1), 0) AS ladder_passed, "
        "COALESCE(SUM(json_extract(scout_json, '$.verdict') = 'take'), 0) AS scout_take, "
        "COALESCE(SUM(json_extract(scout_json, '$.verdict') = 'pass'), 0) AS scout_pass, "
        "COALESCE(SUM(json_extract(trade_json, '$.verdict') = 'execute'), 0) AS trader_execute, "
        "COALESCE(SUM(json_extract(trade_json, '$.verdict') = 'decline'), 0) AS trader_decline, "
        "COALESCE(SUM(outcome = 'risk_rejected' AND trade_json IS NOT NULL), 0) AS final_rejected, "
        "COALESCE(SUM(outcome = 'error'), 0) AS errors "
        "FROM decisions WHERE created_at >= ? AND created_at <= ?",
        (iso(since), iso(until)),
    ).fetchone()
    out = {k: int(row[k] or 0) for k in row.keys()}
    out["executed"] = int(journal.execute(
        "SELECT COUNT(*) FROM trades WHERE created_at >= ? AND created_at <= ?",
        (iso(since), iso(until)),
    ).fetchone()[0])
    return out


def closed_pnl(journal: sqlite3.Connection, since: datetime | None, until: datetime) -> dict:
    """Закрытые сделки окна (по closed_at): P&L сверенный (reconciled=1, филлы биржи) и
    несверенный (оценка монитора) раздельно; без P&L (manual/panic до сверки) — счетчиком."""
    lo = iso(since) if since is not None else ""
    rows = journal.execute(
        "SELECT realized_pnl, reconciled FROM trades "
        "WHERE status = 'closed' AND closed_at >= ? AND closed_at <= ?",
        (lo, iso(until)),
    ).fetchall()
    rec = [float(r["realized_pnl"]) for r in rows if r["reconciled"] and r["realized_pnl"] is not None]
    unrec = [float(r["realized_pnl"]) for r in rows
             if not r["reconciled"] and r["realized_pnl"] is not None]
    known = rec + unrec
    return {
        "closed": len(rows),
        "wins": sum(1 for p in known if p > 0),
        "losses": sum(1 for p in known if p < 0),
        "pnl_reconciled": round(sum(rec), 4), "n_reconciled": len(rec),
        "pnl_unreconciled": round(sum(unrec), 4), "n_unreconciled": len(unrec),
        "n_no_pnl": len(rows) - len(known),
    }


def opened_count(journal: sqlite3.Connection, since: datetime, until: datetime) -> int:
    """Позиции, открывшиеся в окне (вход исполнен по бирже: open или уже closed)."""
    return int(journal.execute(
        "SELECT COUNT(*) FROM trades WHERE status IN ('open','closed') "
        "AND opened_at >= ? AND opened_at <= ? AND entry_price IS NOT NULL",
        (iso(since), iso(until)),
    ).fetchone()[0])


def live_positions(journal: sqlite3.Connection) -> list[dict]:
    """Открытые позиции и ждущие лимитки из журнала (монитор сверяет его с биржей раз в минуту)."""
    rows = journal.execute(
        "SELECT decision_id, symbol, side, status, entry_type, entry_price, sl_price, tp_price, qty, "
        "leverage, risk_usd, opened_at, created_at FROM trades "
        "WHERE status IN ('open','pending') ORDER BY id"
    ).fetchall()
    return [{
        "decision_id": r["decision_id"], "symbol": r["symbol"], "side": r["side"],
        "status": r["status"], "entry_type": r["entry_type"], "entry": r["entry_price"],
        "sl": r["sl_price"], "tp": r["tp_price"], "qty": r["qty"], "leverage": r["leverage"],
        "risk_usd": r["risk_usd"], "opened_at": r["opened_at"], "created_at": r["created_at"],
    } for r in rows]


def llm_usage(journal: sqlite3.Connection, now: datetime, budgets: dict[str, int]) -> dict:
    """Вызовы LLM за 24 ч и остаток дневного бюджета (сутки бюджета — UTC, как в agents/budget.py)."""
    midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    out = {}
    for agent in sorted(set(budgets) | {"scout", "trader"}):
        last24 = int(journal.execute(
            "SELECT COUNT(*) FROM analysis_log WHERE agent = ? AND created_at >= ?",
            (agent, iso(now - timedelta(hours=WINDOW_HOURS))),
        ).fetchone()[0])
        today = int(journal.execute(
            "SELECT COUNT(*) FROM analysis_log WHERE agent = ? AND created_at >= ?",
            (agent, iso(midnight)),
        ).fetchone()[0])
        daily = budgets.get(agent)
        out[agent] = {"calls_24h": last24, "today": today, "daily": daily,
                      "left": None if daily is None else max(0, daily - today)}
    return out


# ------------------------------------------------------------------ сводка


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Состояние демона, которого нет в журнале (собирает daemon при каждой сводке)."""

    run_id: str
    uptime_sec: int
    runner_ticks: int | None = None
    safe_mode: bool = False
    safe_reason: str | None = None
    paused: bool = False
    panic: bool = False
    mode: str | None = None  # auto | confirm | None (переключателя нет)
    equity_usd: float | None = None
    equity_source: str | None = None
    budgets: dict[str, int] = field(default_factory=dict)  # agent -> дневной бюджет


MODE_LABELS = {"auto": "автомат", "confirm": "с подтверждением"}


def _money(v: float) -> str:
    return f"{v:+.2f} $"


def _uptime(sec: int) -> str:
    if sec >= 86400:
        return f"{sec // 86400}д{sec % 86400 // 3600}ч"
    return f"{sec // 3600}ч{sec % 3600 // 60:02d}м"


def _protection_line(journal: sqlite3.Connection, snap: RuntimeSnapshot) -> str:
    from oskar.risk.drawdown import read_halt, read_peak

    if snap.panic:
        state = "PANIC"
    elif snap.safe_mode:
        state = "SAFE MODE" + (f" ({snap.safe_reason})" if snap.safe_reason else "")
    else:
        state = "SAFE MODE нет" + (", пауза" if snap.paused else "")
    halt = read_halt(journal)
    if halt is not None:
        dd = f"СТОП ПО ПРОСАДКЕ −{halt.pct:.1f}% от пика ${halt.peak:.2f} (oskar drawdown-reset)"
    else:
        peak = read_peak(journal)
        if snap.equity_source != "exchange" or not snap.equity_usd:
            dd = "просадка: нет equity с биржи"
        elif not peak:
            dd = "просадка: пик equity еще не записан"
        else:
            dd = f"просадка −{max(0.0, (1 - snap.equity_usd / peak) * 100):.1f}% от пика ${peak:.2f}"
    return f"Защита: {state} · {dd}"


def _invariants_line(verdicts) -> str:
    if verdicts is None:
        return "Инварианты: не проверены"
    if not verdicts:
        return "Инварианты: не объявлены"
    by: dict[str, list[str]] = {}
    for v in verdicts:
        by.setdefault(v.status, []).append(v.id)
    parts = []
    for status, label in (("green", "green"), ("red", "red"), ("insufficient_data", "insufficient"),
                          ("error", "error")):
        ids = by.get(status, [])
        if not ids:
            continue
        shown = f" ({', '.join(ids[:4])})" if status in ("red", "error") else ""
        parts.append(f"{label} {len(ids)}{shown}")
    return "Инварианты: " + " · ".join(parts)


def build_digest(
    journal: sqlite3.Connection,
    now: datetime,
    snap: RuntimeSnapshot,
    *,
    verdicts=None,  # list[ops.invariants.Verdict] | None
    local_now: datetime | None = None,  # для заголовка (МСК); None — UTC
    tz_label: str = "UTC",
) -> str:
    """Сводка за 24 ч (HTML). Динамика (причины, id) экранирована."""
    now = now.astimezone(UTC)
    since = now - timedelta(hours=WINDOW_HOURS)
    f = funnel(journal, since, now)
    pnl = closed_pnl(journal, since, now)
    live = {s: n for s, n in journal.execute(
        "SELECT status, COUNT(*) FROM trades WHERE status IN ('open','pending') GROUP BY status")}
    usage = llm_usage(journal, now, snap.budgets)
    stamp = (local_now or now).strftime("%d.%m %H:%M")

    funnel_line = (f"Воронка: кандидатов {f['candidates']} → лестница {f['ladder_passed']} → "
                   f"Scout take {f['scout_take']} / pass {f['scout_pass']} → "
                   f"Trader execute {f['trader_execute']} → финальная лестница −{f['final_rejected']} "
                   f"→ исполнено {f['executed']}")
    if f["errors"]:
        funnel_line += f" (ошибок/бюджет {f['errors']})"
    pnl_line = (f"P&amp;L: сверенный {_money(pnl['pnl_reconciled'])} ({pnl['n_reconciled']} сд.), "
                f"несверенный {_money(pnl['pnl_unreconciled'])} ({pnl['n_unreconciled']} сд.)")
    if pnl["n_no_pnl"]:
        pnl_line += f", без P&amp;L {pnl['n_no_pnl']}"

    def budget(agent: str, title: str) -> str:
        u = usage[agent]
        left = f", остаток {u['left']}/{u['daily']}" if u["daily"] is not None else ""
        return f"{title} {u['calls_24h']}{left}"

    mode = MODE_LABELS.get(snap.mode or "", "не подключен")
    ticks = "—" if snap.runner_ticks is None else str(snap.runner_ticks)
    lines = [
        f"📊 <b>OSKAR: сводка за 24 ч</b> · {html.escape(stamp)} {html.escape(tz_label)}",
        html.escape(funnel_line, quote=False),
        (f"Сделки: открыто {opened_count(journal, since, now)}, закрыто {pnl['closed']} "
         f"(+{pnl['wins']}/−{pnl['losses']}) · сейчас позиций {live.get('open', 0)}, "
         f"лимиток {live.get('pending', 0)}"),
        pnl_line,
        f"LLM за 24 ч: {budget('scout', 'Scout')} · {budget('trader', 'Trader')}",
        f"Аптайм {_uptime(snap.uptime_sec)}, тиков runner'а {ticks} · режим: {mode}",
        html.escape(_protection_line(journal, snap), quote=False),
        html.escape(_invariants_line(verdicts), quote=False),
    ]
    return "\n".join(lines)
