"""Центральный реестр версий контрактов.

Bump версии здесь И в Literal-поле schema_version соответствующей модели —
одним ревью-коммитом. Тест test_contracts.py сверяет реестр с моделями.
"""

CONTRACT_VERSIONS: dict[str, int] = {
    "SetupCandidate": 1,
    "MarketContext": 1,
    "ScoutReport": 2,  # v2: SetupVerdict.tldr — выжимка для карточки
    "TradeDecision": 2,  # v2: право вето Trader'а (verdict execute|decline)
    "RiskVerdict": 1,
    "ExecutionPlan": 1,
    "ExecutionReport": 1,
    "Lesson": 1,
    "Proposal": 1,
}
