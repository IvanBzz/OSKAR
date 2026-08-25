"""FTS5-индекс библиотеки знаний (vault/library + vault/lessons).

Без embedding-API (ограничение: только подписки) — полнотекстовый поиск SQLite
FTS5 с токенизатором unicode61 (адекватен для русских ключевых слов). Индекс —
производная от vault, пересобирается целиком (`oskar knowledge index`), в git
не попадает (data/).
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = REPO_ROOT / "data" / "knowledge.db"
DEFAULT_SOURCES = [REPO_ROOT / "vault" / "library", REPO_ROOT / "vault" / "lessons"]


@dataclass
class Note:
    path: str
    title: str
    concept: str
    setup_types: str  # csv
    tags: str  # csv
    reliability: str
    body: str


@dataclass
class Hit:
    path: str
    title: str
    concept: str
    setup_types: str
    reliability: str
    snippet: str


def parse_note(path: Path) -> Note | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm: dict = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                fm = {}
            body = parts[2]
    title = path.stem
    try:
        rel = str(path.relative_to(REPO_ROOT))
    except ValueError:  # заметка вне репо (тесты, внешние библиотеки)
        rel = str(path)
    return Note(
        path=rel.replace("\\", "/"),
        title=title,
        concept=str(fm.get("concept", "")),
        setup_types=",".join(fm.get("setup_types", []) or []),
        tags=",".join(fm.get("tags", []) or []),
        reliability=str(fm.get("reliability", "")),
        body=body.strip(),
    )


def build_index(sources: list[Path] | None = None, db_path: Path = DEFAULT_DB) -> int:
    sources = sources or DEFAULT_SOURCES
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS notes;
            CREATE VIRTUAL TABLE notes USING fts5(
                path UNINDEXED, title, concept, setup_types, tags,
                reliability UNINDEXED, body,
                tokenize = 'unicode61 remove_diacritics 2'
            );
            """
        )
        count = 0
        for src in sources:
            if not src.exists():
                continue
            for md in sorted(src.rglob("*.md")):
                note = parse_note(md)
                if note is None or not note.body:
                    continue
                conn.execute(
                    "INSERT INTO notes VALUES (?,?,?,?,?,?,?)",
                    (note.path, note.title, note.concept, note.setup_types,
                     note.tags, note.reliability, note.body),
                )
                count += 1
        conn.commit()
        return count
    finally:
        conn.close()


def _fts_query(raw: str) -> str:
    """Пользовательский ввод -> безопасный FTS5-запрос: каждое слово в кавычках, OR."""
    words = [w for w in raw.replace('"', " ").split() if w]
    return " OR ".join(f'"{w}"' for w in words)


def search(query: str, db_path: Path = DEFAULT_DB, limit: int = 8) -> list[Hit]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT path, title, concept, setup_types, reliability,
                   snippet(notes, 6, '>>', '<<', ' ... ', 12)
            FROM notes WHERE notes MATCH ? ORDER BY rank LIMIT ?
            """,
            (_fts_query(query), limit),
        ).fetchall()
        return [Hit(*row) for row in rows]
    finally:
        conn.close()
