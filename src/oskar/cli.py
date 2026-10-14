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
    _add_daemon_api_parsers(sub)  # ст. 6: panic / panic-reset / status через API демона

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
    p_rec = sync_sub.add_parser(
        "reconcile", help="closed trades vs exchange fills (Art. 1); writes sync_snapshots"
    )
    p_rec.add_argument("--dry-run", action="store_true", help="report only, journal untouched")

    p_inv = sub.add_parser("invariants", help="Art. 5 verdicts from the journal (exit 1 on red)")
    p_inv.add_argument("--no-note", action="store_true", help="do not write a note to vault/runs/")

    sub.add_parser(
        "buttons-ping",
        help="send a self-test card via the signals bot (press -> 'buttons work')",
    )

    args = parser.parse_args()

    if args.command in DAEMON_API_COMMANDS:
        return _cmd_daemon_api(args.command)

    if args.command == "sync":
        return _cmd_sync(args)

    if args.command == "buttons-ping":
        return _cmd_buttons_ping()

    if args.command == "invariants":
        return _cmd_invariants(args)

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


# ------------------------------------------------------------------ oskard API
# Ст. 6 (ревью 2026-10-13): panic обещан и из CLI, а был только API. Ниже — тонкий
# клиент локального API демона: stdlib urllib и без bootstrap (он тянет pandas ~4 с),
# panic из терминала должен уходить за доли секунды.

DAEMON_API_COMMANDS = {
    "panic": ("POST", "/panic", 60.0),  # снятие ордеров/закрытие позиций на бирже — до минуты
    "panic-reset": ("POST", "/panic/reset", 10.0),
    "status": ("GET", "/status", 10.0),
}


def _add_daemon_api_parsers(sub) -> None:
    sub.add_parser("panic", help="EMERGENCY STOP (Art. 6): cancel entries, close positions, safe_mode")
    sub.add_parser("panic-reset", help="lift panic: explicit operator command only (Art. 6.3); pause stays")
    sub.add_parser("status", help="oskard state via the local API")


def _ascii(value) -> str:
    """cp1251-консоль: только ASCII (кириллица ошибок биржи -> \\u-escape, а не кракозябры)."""
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


def _daemon_api_base() -> str:
    """Адрес API — из settings.api, как у демона; сбой чтения конфига -> 127.0.0.1:8321:
    panic не имеет права упасть на YAML."""
    from oskar.core.daemon import DEFAULT_HOST, DEFAULT_PORT, REPO_ROOT, api_endpoint

    try:
        import yaml

        settings = yaml.safe_load((REPO_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
        host, port = api_endpoint(settings or {})
    except Exception:  # noqa: BLE001
        host, port = DEFAULT_HOST, DEFAULT_PORT
    return f"http://{host}:{port}"


def _daemon_token_file():
    from oskar.core.daemon import REPO_ROOT

    return REPO_ROOT / "data" / "oskard.token"


def _daemon_api_tokens() -> list[str]:
    """X-Oskar-Token: env OSKAR_API_TOKEN, затем data/oskard.token (пишет oskard при старте),
    как в skills/oskar-control/scripts/oskarctl.py. Берем оба: устаревший env не должен
    сорвать panic (401 -> пробуем токен из файла)."""
    import os

    tokens = []
    env_token = os.getenv("OSKAR_API_TOKEN", "").strip()
    if env_token:
        tokens.append(env_token)
    try:
        file_token = _daemon_token_file().read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        file_token = ""
    if file_token and file_token not in tokens:
        tokens.append(file_token)
    return tokens or [""]


def _daemon_api_call(method: str, path: str, timeout: float) -> tuple[int, object]:
    """(HTTP-код, JSON или текст). Код 0 — демон не ответил (нет соединения / таймаут)."""
    import json
    import urllib.error
    import urllib.request

    base = _daemon_api_base()
    # loopback мимо системного/env-прокси (VPN): иначе panic уходил бы в прокси, а не в демон
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    status, body = 0, f"{base}: no tokens"
    for token in _daemon_api_tokens():
        req = urllib.request.Request(base + path, method=method, headers={"X-Oskar-Token": token})
        if method == "POST":
            req.data = b""
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw)
                except ValueError:
                    return resp.status, raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read().decode("utf-8", errors="replace")
            if e.code != 401:
                break  # 401 — пробуем следующий токен; прочие коды — окончательный ответ
        except OSError as e:  # URLError (отказ соединения), таймаут чтения
            return 0, f"{base}: {getattr(e, 'reason', e)}"
    return status, body


def _cmd_daemon_api(command: str) -> int:
    import json

    method, path, timeout = DAEMON_API_COMMANDS[command]
    status, body = _daemon_api_call(method, path, timeout)
    if status == 0:
        print(f"OSKARD NOT RUNNING? no answer from {_ascii(body)}")
        if command == "panic":
            print("PANIC NOT APPLIED by oskard: cancel orders / close positions in the BingX UI now")
        return 1
    if status != 200 or not isinstance(body, dict):
        print(f"HTTP {status}: {_ascii(body)[:300]}")
        if status == 401:
            print("token rejected: check OSKAR_API_TOKEN / data/oskard.token")
        return 1
    if command == "panic":
        return _print_panic(body)
    if command == "panic-reset":
        print(f"panic reset: safe_mode={body.get('safe_mode')} paused={body.get('paused')} "
              "(pause stays - resume is a separate explicit decision)")
        return 0
    print(json.dumps(body, ensure_ascii=True, indent=2))
    return 0


def _print_panic(result: dict) -> int:
    """Сводка panic; код 1, если биржа сделала не всё или флаг не записан."""
    print("PANIC ENGAGED: safe_mode + pause, new entries blocked until `oskar panic-reset`")
    ok = True
    exchange = result.get("exchange")
    if exchange is None:
        print("exchange: NOT touched - oskard has no position monitor (no exchange keys)")
    elif "error" in exchange:
        ok = False
        print(f"exchange: ERROR {_ascii(exchange['error'])} - check positions in the BingX UI")
    else:
        errors = exchange.get("errors") or []
        print(f"exchange: cancelled={exchange.get('cancelled', 0)} closed={exchange.get('closed', 0)} "
              f"errors={len(errors)}")
        for err in errors:
            ok = False
            print(f"  error: {_ascii(err)}")
    if result.get("persist_error"):
        ok = False
        print(f"WARNING: panic flag not persisted ({_ascii(result['persist_error'])}) "
              "- a restart would lift it")
    return 0 if ok else 1


def _cmd_invariants(args) -> int:
    """Ст. 5: вердикты инвариантов по журналу + заметка в vault/runs/ (ревью 2026-10-13, Codex #27)."""
    from pathlib import Path

    from oskar.ops.invariants import cli_main

    return cli_main(Path(__file__).resolve().parents[2] / "data" / "oskar.db", write_note=not args.no_note)


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
    from oskar.core.bootstrap import load_settings

    # та же среда, что у демона (settings.exchange.sandbox), а не зашитый VST
    sandbox = bool(load_settings().get("exchange", {}).get("sandbox", True))
    view = make_bingx_view(key, secret, sandbox=sandbox)

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
    if args.sync_command == "reconcile":
        # ст. 1 п. 1: закрытые сделки vs филлы биржи (ревью 2026-10-13, Claudexor H10)
        from oskar.sync.reconcile import cli_reconcile

        return cli_reconcile(view, repo / "data" / "oskar.db", write=not args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
