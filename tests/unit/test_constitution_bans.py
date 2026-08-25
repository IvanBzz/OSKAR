"""Ст. 4 п. 3: запрещенные конструкции не существуют в кодовой базе."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BANNED = "force" + "_trade"  # собрано из частей, чтобы тест не ловил сам себя
SCAN_DIRS = ["src", "config", "skills", "scripts"]
SCAN_SUFFIXES = {".py", ".yaml", ".yml", ".toml", ".md", ".json"}


def test_no_banned_flags_anywhere():
    hits = []
    for d in SCAN_DIRS:
        root = REPO / d
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix in SCAN_SUFFIXES and path.is_file():
                text = path.read_text(encoding="utf-8", errors="ignore")
                if BANNED in text:
                    hits.append(str(path.relative_to(REPO)))
    assert not hits, f"banned flag found in: {hits} (CONSTITUTION.md Art. 4.3)"
