"""Нормализатор символов: все форматы, виденные в v1, + edge cases."""

import pytest

from oskar.data.symbols import (
    SymbolError,
    canonical,
    is_canonical,
    to_display,
    to_exchange,
)

# Каждый формат, встречавшийся в v1 (LLM-ответы, ccxt, BingX, журнал)
V1_SEEN = [
    ("SOLUSDT", "SOL/USDT:USDT"),
    ("SOL-USDT", "SOL/USDT:USDT"),
    ("SOL/USDT", "SOL/USDT:USDT"),
    ("SOL/USDT:USDT", "SOL/USDT:USDT"),
    ("BTC/USDT:USDT", "BTC/USDT:USDT"),
    ("ADA-USDT", "ADA/USDT:USDT"),
    ("ada/usdt", "ADA/USDT:USDT"),
    (" ETHUSDT ", "ETH/USDT:USDT"),
    ("SUSHI-USDT", "SUSHI/USDT:USDT"),
    ("1000PEPEUSDT", "1000PEPE/USDT:USDT"),
    ("1INCHUSDT", "1INCH/USDT:USDT"),
    ("ONE_USDT", "ONE/USDT:USDT"),
]


@pytest.mark.parametrize(("raw", "expected"), V1_SEEN)
def test_canonical_all_v1_formats(raw, expected):
    assert canonical(raw) == expected


def test_canonical_idempotent():
    for raw, _ in V1_SEEN:
        c = canonical(raw)
        assert canonical(c) == c
        assert is_canonical(c)


def test_duplicate_detection_works_after_normalization():
    """Сценарий бага v1: ADA/USDT:USDT и ADA-USDT — ОДНА позиция."""
    assert canonical("ADA/USDT:USDT") == canonical("ADA-USDT") == canonical("ADAUSDT")


def test_to_exchange_bingx_format():
    assert to_exchange("SOL/USDT:USDT") == "SOL-USDT"
    assert to_exchange("1000PEPE/USDT:USDT") == "1000PEPE-USDT"


def test_to_display():
    assert to_display("SOL/USDT:USDT") == "SOL/USDT"


def test_rejects_garbage():
    for bad in ["", "   ", "USDT", "???", "SOL", "SOL/XYZ", "SOL/USDT:BTC"]:
        with pytest.raises(SymbolError):
            canonical(bad)


def test_non_canonical_input_to_accessors_raises():
    with pytest.raises(SymbolError):
        to_exchange("SOLUSDT")  # не канон — сначала canonical()
