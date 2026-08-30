"""Контракты: round-trip, запреты схемы, реестр версий, дрейф экспорта."""

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from oskar.contracts import CONTRACT_VERSIONS
from oskar.contracts.schemas import (
    PriceOption,
    SetupCandidate,
    SetupFeatures,
    SetupVerdict,
)

REPO = Path(__file__).resolve().parents[2]


def make_candidate(**overrides) -> SetupCandidate:
    base = dict(
        id="sc-001",
        ts=datetime.now(UTC),
        symbol="SOL/USDT:USDT",
        direction="short",
        setup_type="fvg",
        timeframe="15m",
        score=78,
        features=SetupFeatures(rsi=61.2, trend_mtf="bearish"),
        entry_options=[PriceOption(id="e1", kind="limit_fvg_edge", price=142.10)],
        sl_options=[PriceOption(id="s1", kind="swing_low", price=143.80)],
        tp_options=[PriceOption(id="t1", kind="liquidity_pool", price=138.60)],
    )
    base.update(overrides)
    return SetupCandidate(**base)


def test_candidate_roundtrip():
    c = make_candidate()
    restored = SetupCandidate.model_validate_json(c.model_dump_json())
    assert restored == c
    assert restored.schema_version == 1


def test_confidence_cap_enforced_at_schema_level():
    """Ст. 3 п. 5: confidence > 75 отклоняется контрактом, не полиси-кодом."""
    ok = dict(
        candidate_id="sc-001",
        tldr="выжимка сетапа одной фразой",
        bull_case="x" * 30,
        bear_case="y" * 30,
        bear_rebuttal="z" * 30,
        verdict="take",
    )
    assert SetupVerdict(**ok, confidence=75).confidence == 75
    with pytest.raises(ValidationError):
        SetupVerdict(**ok, confidence=76)


def test_confidence_cap_matches_caps_yaml():
    """Схемный кап и caps.yaml не должны разъехаться."""
    import yaml

    from oskar.contracts.schemas import CONFIDENCE_CAP

    caps = yaml.safe_load((REPO / "config" / "caps.yaml").read_text(encoding="utf-8"))
    assert CONFIDENCE_CAP == caps["confidence_cap"]


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        make_candidate(unknown_field="boom")


def test_empty_option_lists_rejected():
    """Trader выбирает из меню — пустое меню недопустимо."""
    with pytest.raises(ValidationError):
        make_candidate(entry_options=[])


def make_trade_decision(**overrides):
    from oskar.contracts.schemas import TradeDecision

    base = dict(
        decision_id="d1", ts=datetime.now(UTC), candidate_id="sc-001",
        verdict="execute", entry_option_id="e-market", sl_option_id="s-swing",
        tp_option_id="t-liq", rationale="вход от края FVG, стоп за свингом",
        model="fable", prompt_sha="x" * 64,
    )
    base.update(overrides)
    return TradeDecision(**base)


def test_trade_decision_execute_roundtrip():
    d = make_trade_decision()
    from oskar.contracts.schemas import TradeDecision

    assert TradeDecision.model_validate_json(d.model_dump_json()) == d
    assert d.schema_version == 2


def test_trade_decision_decline_requires_reason():
    """v2: вето Trader'а — decline обязан объяснить, execute обязан выбрать."""
    ok = make_trade_decision(
        verdict="decline", entry_option_id=None, sl_option_id=None,
        tp_option_id=None,
        decline_reason="вход технически кривой: до пула ликвидности полшага",
    )
    assert ok.verdict == "decline"
    with pytest.raises(ValidationError, match="decline_reason"):
        make_trade_decision(
            verdict="decline", entry_option_id=None, sl_option_id=None,
            tp_option_id=None,
        )
    with pytest.raises(ValidationError, match="option ids"):
        make_trade_decision(verdict="decline", decline_reason="причина достаточной длины тут")
    with pytest.raises(ValidationError, match="option ids"):
        make_trade_decision(entry_option_id=None)


def test_version_registry_matches_models():
    import oskar.contracts as contracts

    for name, version in CONTRACT_VERSIONS.items():
        model = getattr(contracts, name)
        field = model.model_fields["schema_version"]
        assert field.default == version, f"{name}: registry={version}, model={field.default}"


def test_exported_schemas_not_drifted():
    """JSON-схемы в git соответствуют живым моделям (запуск scripts/export_schemas.py)."""
    sys.path.insert(0, str(REPO / "scripts"))
    from export_schemas import OUT_DIR, TOP_LEVEL, render

    for model in TOP_LEVEL:
        path = OUT_DIR / f"{model.__name__}.schema.json"
        assert path.exists(), f"missing export: {path.name} — run scripts/export_schemas.py"
        assert path.read_text(encoding="utf-8").replace("\r\n", "\n") == render(model), (
            f"drift in {path.name} — run scripts/export_schemas.py"
        )
