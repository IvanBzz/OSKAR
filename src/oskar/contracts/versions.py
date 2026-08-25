"""Центральный реестр версий контрактов.

Bump версии здесь И в Literal-поле schema_version соответствующей модели —
одним ревью-коммитом. Тест test_contracts.py сверяет реестр с моделями.
"""

CONTRACT_VERSIONS: dict[str, int] = {
    "SetupCandidate": 1,
    "MarketContext": 1,
    "ScoutReport": 1,
    "TradeDecision": 1,
    "RiskVerdict": 1,
    "ExecutionReport": 1,
    "Lesson": 1,
    "Proposal": 1,
}
