"""Установка git-хуков OSKAR: пишет .git/hooks/pre-commit (sh-шим на Python-чек)."""

import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

SHIM = """#!/bin/sh
# OSKAR pre-commit (installed by scripts/hooks/install_hooks.py)
python "$(git rev-parse --show-toplevel)/scripts/hooks/pre_commit_check.py" || exit 1
"""


def main() -> int:
    hooks_dir = REPO / ".git" / "hooks"
    if not hooks_dir.exists():
        print("ERROR: .git/hooks not found — run from a git checkout")
        return 1
    target = hooks_dir / "pre-commit"
    target.write_text(SHIM, encoding="utf-8", newline="\n")
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    print(f"installed: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
