"""Frozen contracts: типизированные схемы всех сообщений между компонентами.

Паттерн заимствован из Ouroboros (contracts/tool_abi.py + schema_versions.py):
минимальный замороженный протокол, явный реестр версий, изменение схемы =
bump версии + ревью-коммит (CONSTITUTION.md ст. 2 п. 4).
"""

from oskar.contracts.schemas import (
    PROMPT_EXCLUDE,
    CandleFrame,
    CandleShadow,
    ExecutionPlan,
    ExecutionReport,
    Fill,
    Lesson,
    MarketContext,
    OrderRef,
    PriceOption,
    Proposal,
    RiskComputed,
    RiskStep,
    RiskVerdict,
    ScoutReport,
    SetupCandidate,
    SetupFeatures,
    SetupVerdict,
    TradeDecision,
)
from oskar.contracts.versions import CONTRACT_VERSIONS

__all__ = [
    "CONTRACT_VERSIONS",
    "PROMPT_EXCLUDE",
    "CandleFrame",
    "CandleShadow",
    "ExecutionPlan",
    "ExecutionReport",
    "Fill",
    "Lesson",
    "MarketContext",
    "OrderRef",
    "PriceOption",
    "Proposal",
    "RiskComputed",
    "RiskStep",
    "RiskVerdict",
    "ScoutReport",
    "SetupCandidate",
    "SetupFeatures",
    "SetupVerdict",
    "TradeDecision",
]
