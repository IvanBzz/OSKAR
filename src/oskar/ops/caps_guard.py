"""Страж конституционных капсов (CONSTITUTION.md ст. 4).

Хеш = sha256 над UTF-8 байтами caps.yaml с нормализацией CRLF->LF
(git autocrlf не должен ломать пин между платформами).
Рантайм (daemon) вызывает verify_caps() при старте: несовпадение = safe_mode.
"""

import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CAPS_PATH = REPO_ROOT / "config" / "caps.yaml"
CONSTITUTION_PATH = REPO_ROOT / "CONSTITUTION.md"

_PIN_RE = re.compile(r"sha256:\s*([0-9a-f]{64})")


def caps_hash(path: Path = CAPS_PATH) -> str:
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def pinned_hash(path: Path = CONSTITUTION_PATH) -> str:
    text = path.read_text(encoding="utf-8")
    m = _PIN_RE.search(text)
    if not m:
        raise RuntimeError("CONSTITUTION.md: sha256-pin for caps.yaml not found")
    return m.group(1)


def verify_caps() -> bool:
    """True = caps.yaml не тронут в обход процедуры ст. 4."""
    return caps_hash() == pinned_hash()
