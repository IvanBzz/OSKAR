"""Pre-commit проверки OSKAR (CONSTITUTION.md ст. 4.3, ст. 7.2).

1. Коммит, меняющий src/ или config/, обязан менять vault/progress/changelog.md.
2. Запрещенная строка (флаг принудительной торговли) не входит в staged-файлы.

Установка: uv run python scripts/hooks/install_hooks.py
"""

import subprocess
import sys

BANNED = "force" + "_trade"
CHANGELOG = "vault/progress/changelog.md"


def staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, check=True,
    )
    return [line.strip().replace("\\", "/") for line in out.stdout.splitlines() if line.strip()]


def staged_content(path: str) -> str:
    out = subprocess.run(["git", "show", f":{path}"], capture_output=True, check=True)
    return out.stdout.decode("utf-8", errors="ignore")


def main() -> int:
    files = staged_files()
    errors = []

    touches_code = [f for f in files if f.startswith(("src/", "config/"))]
    if touches_code and CHANGELOG not in files:
        errors.append(
            "Commit changes src/ or config/ but not vault/progress/changelog.md.\n"
            "  Art. 7.2: vault lagging behind code is documentation lying.\n"
            f"  Touched: {', '.join(touches_code[:5])}"
        )

    for f in files:
        if f.startswith(("src/", "config/", "skills/", "scripts/")) and BANNED in staged_content(f):
            errors.append(f"Banned flag '{BANNED}' in staged file: {f} (Art. 4.3)")

    if errors:
        print("PRE-COMMIT REJECTED:")
        for e in errors:
            print(f"- {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
