"""CLI OSKAR. Фаза 0: только caps-verify. Далее: sync, panic, knowledge и др.

Вывод ASCII-only: Windows-консоль с cp1251 роняет unicode (урок v1).
"""

import argparse
import sys

from oskar.ops.caps_guard import caps_hash, pinned_hash, verify_caps


def main() -> int:
    parser = argparse.ArgumentParser(prog="oskar", description="OSKAR control CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("caps-verify", help="verify caps.yaml against CONSTITUTION.md pin")

    p_know = sub.add_parser("knowledge", help="knowledge index operations")
    know_sub = p_know.add_subparsers(dest="know_command", required=True)
    know_sub.add_parser("index", help="rebuild FTS5 index from vault/library + vault/lessons")
    p_search = know_sub.add_parser("search", help="search the knowledge index")
    p_search.add_argument("query", nargs="+")
    p_search.add_argument("--limit", type=int, default=8)

    args = parser.parse_args()

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


if __name__ == "__main__":
    sys.exit(main())
