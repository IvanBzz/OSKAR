"""CLI OSKAR. Фаза 0: только caps-verify. Далее: sync, panic, knowledge и др.

Вывод ASCII-only: Windows-консоль с cp1251 роняет unicode (урок v1).
"""

import argparse
import sys

from oskar.ops.caps_guard import caps_hash, pinned_hash, verify_caps


def main() -> int:
    from oskar._io import force_utf8_stdio

    force_utf8_stdio()
    parser = argparse.ArgumentParser(prog="oskar", description="OSKAR control CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("caps-verify", help="verify caps.yaml against CONSTITUTION.md pin")

    p_know = sub.add_parser("knowledge", help="knowledge index operations")
    know_sub = p_know.add_subparsers(dest="know_command", required=True)
    know_sub.add_parser("index", help="rebuild FTS5 index from vault/library + vault/lessons")
    p_search = know_sub.add_parser("search", help="search the knowledge index")
    p_search.add_argument("query", nargs="+")
    p_search.add_argument("--limit", type=int, default=8)

    p_sync = sub.add_parser("sync", help="read-only exchange window (exists BEFORE first trade)")
    sync_sub = p_sync.add_subparsers(dest="sync_command", required=True)
    sync_sub.add_parser("balance", help="exchange balance snapshot")
    sync_sub.add_parser("positions", help="open positions on exchange")
    sync_sub.add_parser("diff", help="journal vs exchange discrepancy report")

    sub.add_parser(
        "buttons-ping",
        help="send a self-test card via the signals bot (press -> 'buttons work')",
    )

    args = parser.parse_args()

    if args.command == "sync":
        return _cmd_sync(args)

    if args.command == "buttons-ping":
        return _cmd_buttons_ping()

    if args.command == "knowledge":
        from oskar.knowledge.index import build_index, search

        if args.know_command == "index":
            count = build_index()
            print(f"indexed {count} notes")
            return 0
        if args.know_command == "search":
            hits = search(" ".join(args.query), limit=args.limit)
            if not hits:
                print("no hits")
                return 1
            for h in hits:
                print(f"[{h.setup_types or '-'}] {h.path}")
                print(f"    {h.snippet}")
            return 0

    if args.command == "caps-verify":
        actual, pinned = caps_hash(), pinned_hash()
        if verify_caps():
            print(f"OK: caps.yaml matches constitution pin ({actual[:12]}...)")
            return 0
        print("VIOLATION: caps.yaml does not match CONSTITUTION.md pin!")
        print(f"  pinned: {pinned}")
        print(f"  actual: {actual}")
        print("  Amendment procedure (Art. 4) required. Runtime would enter safe_mode.")
        return 1
    return 0


def _cmd_buttons_ping() -> int:
    """Карточка-самопроверка кнопок: доходит ли нажатие до поллера демона."""
    import os
    from pathlib import Path

    from dotenv import load_dotenv

    from oskar.core.telegram_buttons import send_ping_card

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    token, chat = os.getenv("TELEGRAM_SIGNALS_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("ERROR: TELEGRAM_SIGNALS_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return 1
    if send_ping_card(token, chat):
        print("ping card sent - press the button; oskard must be running to answer")
        return 0
    print("ERROR: telegram refused the message")
    return 1


def _cmd_sync(args) -> int:
    import os
    from pathlib import Path

    from dotenv import load_dotenv

    from oskar.sync.exchange_sync import (
        diff_positions,
        exchange_position_qty,
        make_bingx_view,
        render_diff,
        snapshot_balance,
    )

    repo = Path(__file__).resolve().parents[2]
    load_dotenv(repo / ".env")
    key, secret = os.getenv("BINGX_API_KEY", ""), os.getenv("BINGX_API_SECRET", "")
    if not key or not secret:
        print("ERROR: BINGX_API_KEY / BINGX_API_SECRET not set (see .env.example)")
        return 1
    view = make_bingx_view(key, secret, sandbox=True)

    if args.sync_command == "balance":
        b = snapshot_balance(view)
        print(f"{b['currency']} total={b['total']:.2f} free={b['free']:.2f} used={b['used']:.2f}")
        return 0
    if args.sync_command == "positions":
        positions = view.positions()
        if not positions:
            print("no open positions")
            return 0
        for p in positions:
            qty = exchange_position_qty(p)
            if qty == 0:
                continue
            print(f"{p.get('symbol')} {p.get('side')} qty={qty:.8g} "
                  f"entry={p.get('entryPrice')} upnl={p.get('unrealizedPnl')}")
        return 0
    if args.sync_command == "diff":
        from oskar.journal.db import connect

        conn = connect(repo / "data" / "oskar.db")
        try:
            d = diff_positions(view, conn)
            print(render_diff(d))
            return 0 if d.clean else 1
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
