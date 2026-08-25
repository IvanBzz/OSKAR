"""Экспорт JSON Schema контрактов в src/oskar/contracts/json/ (детерминированно).

Запуск: uv run python scripts/export_schemas.py
Тест test_contracts.py::test_exported_schemas_not_drifted падает, если экспорт
не соответствует живым моделям — схемы в git всегда актуальны.
"""

import json
from pathlib import Path

from oskar.contracts import (
    ExecutionReport,
    Lesson,
    MarketContext,
    Proposal,
    RiskVerdict,
    ScoutReport,
    SetupCandidate,
    TradeDecision,
)

TOP_LEVEL = [
    SetupCandidate,
    MarketContext,
    ScoutReport,
    TradeDecision,
    RiskVerdict,
    ExecutionReport,
    Lesson,
    Proposal,
]

OUT_DIR = Path(__file__).resolve().parents[1] / "src" / "oskar" / "contracts" / "json"


def render(model) -> str:
    schema = model.model_json_schema()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for model in TOP_LEVEL:
        path = OUT_DIR / f"{model.__name__}.schema.json"
        path.write_text(render(model), encoding="utf-8", newline="\n")
        print(f"exported {path.name}")


if __name__ == "__main__":
    main()
