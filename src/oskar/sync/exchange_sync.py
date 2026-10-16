"""exchange_sync — read-only окно в биржу (CONSTITUTION.md ст. 1 п. 3).

Принципы v1 (vault: exchange_sync — read-only окно в BingX VST):
- read-only по умолчанию, никакого скрытого состояния — свежий вызов API каждый раз
- ASCII-only вывод (Windows cp1251)
- именно этот инструмент вскрыл в v1 фантомные -$329 против реальных -$7.56

Функции чистые: принимают ExchangeView (протокол) — реальный ccxt-BingX
подключается фабрикой make_bingx_view(), тесты используют фейк.

diff — детектор qty-бага v1: сверяет qty открытых сделок журнала с
contracts * contractSize позиций биржи, per-symbol ratio.

ЗАКРЫТЫЕ сделки сверяются по истории филлов в sync/reconcile.py (`oskar sync
reconcile`); view отдает для этого fetch_my_trades.
"""

import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from oskar.data.symbols import canonical

QTY_TOLERANCE = 0.001  # |ratio - 1| выше — расхождение


class ExchangeView(Protocol):
    """Read-only срез биржи. Реализации: ccxt BingX (live/VST), фейки в тестах."""

    def balance(self) -> dict[str, Any]: ...
    def positions(self) -> list[dict[str, Any]]: ...
    def open_orders(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class QtyDiff:
    symbol: str
    journal_qty: float
    exchange_qty: float
    ratio: float | None  # None при нулевом журнальном qty
    ok: bool


@dataclass(frozen=True)
class PositionDiff:
    missing_on_exchange: list[str]  # символ+сторона есть в журнале, нет на бирже
    missing_in_journal: list[str]   # есть на бирже, нет в журнале
    qty_diffs: list[QtyDiff]

    @property
    def clean(self) -> bool:
        return (
            not self.missing_on_exchange
            and not self.missing_in_journal
            and all(d.ok for d in self.qty_diffs)
        )


def snapshot_balance(view: ExchangeView) -> dict:
    """Баланс USDT или VST (демо-валюта BingX VST называется 'VST', не USDT)."""
    raw = view.balance()
    for cur in ("USDT", "VST"):
        entry = raw.get(cur)
        if isinstance(entry, dict) and entry.get("total") is not None:
            return {
                "currency": cur,
                "total": float(entry.get("total") or 0.0),
                "free": float(entry.get("free") or 0.0),
                "used": float(entry.get("used") or 0.0),
            }
    totals = raw.get("total") or {}
    for cur in ("USDT", "VST"):
        if totals.get(cur) is not None:
            return {
                "currency": cur,
                "total": float(totals.get(cur) or 0.0),
                "free": float((raw.get("free") or {}).get(cur) or 0.0),
                "used": float((raw.get("used") or {}).get(cur) or 0.0),
            }
    return {"currency": "USDT", "total": 0.0, "free": 0.0, "used": 0.0}


def exchange_position_qty(pos: dict[str, Any]) -> float:
    """contracts * contractSize — реальное количество базовой валюты.

    Гипотеза qty-бага v1: journal писал qty в одних единицах, биржа отдавала
    contracts в других; ratio 6.98x-833x per-symbol = contractSize-shaped.
    """
    contracts = float(pos.get("contracts") or 0.0)
    contract_size = float(pos.get("contractSize") or 1.0)
    return contracts * contract_size


def diff_positions(view: ExchangeView, journal: sqlite3.Connection) -> PositionDiff:
    """Открытые сделки журнала vs позиции биржи: наличие + qty per symbol+side."""
    ex_positions: dict[tuple[str, str], float] = {}
    for p in view.positions():
        qty = exchange_position_qty(p)
        if qty == 0:
            continue
        side = str(p.get("side", "")).lower()  # long | short
        key = (canonical(p["symbol"]), side)
        ex_positions[key] = ex_positions.get(key, 0.0) + qty

    j_positions: dict[tuple[str, str], float] = {}
    rows = journal.execute(
        "SELECT symbol, side, qty FROM trades WHERE status = 'open'"
    ).fetchall()
    for r in rows:
        key = (canonical(r["symbol"]), r["side"])
        j_positions[key] = j_positions.get(key, 0.0) + float(r["qty"] or 0.0)

    missing_on_exchange = [
        f"{s} {side}" for (s, side) in j_positions if (s, side) not in ex_positions
    ]
    missing_in_journal = [
        f"{s} {side}" for (s, side) in ex_positions if (s, side) not in j_positions
    ]

    qty_diffs: list[QtyDiff] = []
    for key in sorted(set(j_positions) & set(ex_positions)):
        jq, eq = j_positions[key], ex_positions[key]
        ratio = eq / jq if jq else None
        ok = ratio is not None and abs(ratio - 1.0) <= QTY_TOLERANCE
        qty_diffs.append(
            QtyDiff(symbol=f"{key[0]} {key[1]}", journal_qty=jq,
                    exchange_qty=eq, ratio=ratio, ok=ok)
        )

    return PositionDiff(
        missing_on_exchange=sorted(missing_on_exchange),
        missing_in_journal=sorted(missing_in_journal),
        qty_diffs=qty_diffs,
    )


def render_diff(d: PositionDiff) -> str:
    """ASCII-отчет."""
    lines = []
    if d.clean:
        lines.append("DIFF CLEAN: journal == exchange")
    else:
        lines.append("DIFF DIRTY:")
        for m in d.missing_on_exchange:
            lines.append(f"  [journal-only]  {m} - open in journal, absent on exchange")
        for m in d.missing_in_journal:
            lines.append(f"  [exchange-only] {m} - position on exchange, not in journal")
    for q in d.qty_diffs:
        mark = "ok" if q.ok else "MISMATCH"
        ratio = f"{q.ratio:.4f}" if q.ratio is not None else "n/a"
        lines.append(
            f"  [{mark}] {q.symbol}: journal={q.journal_qty:.8g} "
            f"exchange={q.exchange_qty:.8g} ratio={ratio}"
        )
    return "\n".join(lines)


def make_bingx_view(api_key: str, secret: str, sandbox: bool = True) -> ExchangeView:
    """Read-only view поверх ccxt BingX — только VST до гейта Фазы 5 (блок H, 2026-10-16:
    sandbox — именно True, иначе исключение, а не реальный аккаунт)."""
    import ccxt

    from oskar.core.env_gate import require_vst

    require_vst(sandbox)
    ex = ccxt.bingx({
        "apiKey": api_key,
        "secret": secret,
        # defaultType=swap: иначе balance уходит в SPOT реального аккаунта
        "options": {"adjustForTimeDifference": True, "defaultType": "swap"},
    })
    ex.set_sandbox_mode(True)

    class _CcxtView:
        def balance(self) -> dict[str, Any]:
            return ex.fetch_balance()

        def positions(self) -> list[dict[str, Any]]:
            return ex.fetch_positions()

        def open_orders(self) -> list[dict[str, Any]]:
            return ex.fetch_open_orders()

        def fetch_my_trades(
            self, symbol: str, since: int | None = None, limit: int | None = None,
            params: dict | None = None,
        ) -> list[dict[str, Any]]:
            # история исполнений для сверки ЗАКРЫТЫХ сделок (sync/reconcile.py, ст. 1 п. 1;
            # ревью 2026-10-13, Claudexor H10): тоже только чтение
            return ex.fetch_my_trades(symbol, since, limit, params or {})

    return _CcxtView()
