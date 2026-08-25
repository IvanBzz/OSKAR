"""FTS5-индекс: сборка, поиск по русским и английским ключевым словам."""

from pathlib import Path

from oskar.knowledge.index import build_index, parse_note, search

NOTE = """---
tags: [library, smc]
concept: fvg-entry-edge
source: "Smart Money Concept.pdf"
pages: "40-55"
setup_types: [fvg]
reliability: book_claim
date: 2026-08-25
---
# FVG валиден пока не митигирован — вход от края, не от середины

Имбаланс (fair value gap) образуется трехсвечной формацией. Вход планируется
от границы гэпа. После полного перекрытия FVG считается митигированным.

## Связи
- [[order block — последняя свеча против импульса]]
"""


def make_vault(tmp_path: Path) -> Path:
    lib = tmp_path / "library" / "smc"
    lib.mkdir(parents=True)
    (lib / "FVG валиден пока не митигирован — вход от края, не от середины.md").write_text(
        NOTE, encoding="utf-8"
    )
    return tmp_path / "library"


def test_parse_note_extracts_frontmatter(tmp_path: Path):
    lib = make_vault(tmp_path)
    note = parse_note(next(lib.rglob("*.md")))
    assert note.concept == "fvg-entry-edge"
    assert note.setup_types == "fvg"
    assert note.reliability == "book_claim"
    assert "митигированным" in note.body


def test_build_and_search_russian(tmp_path: Path):
    lib = make_vault(tmp_path)
    db = tmp_path / "knowledge.db"
    assert build_index([lib], db) == 1
    hits = search("митигирован имбаланс", db)
    assert hits and hits[0].concept == "fvg-entry-edge"


def test_search_english_term(tmp_path: Path):
    lib = make_vault(tmp_path)
    db = tmp_path / "knowledge.db"
    build_index([lib], db)
    assert search("fair value gap", db)


def test_search_no_hits(tmp_path: Path):
    lib = make_vault(tmp_path)
    db = tmp_path / "knowledge.db"
    build_index([lib], db)
    assert search("волновой эллиотт", db) == []
