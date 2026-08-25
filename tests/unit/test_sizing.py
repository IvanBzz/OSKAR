"""Сайзинг: формулы v1 против воркед-примеров из vault (dynamic-leverage.md)."""

import pytest

from oskar.risk.sizing import (
    dynamic_leverage,
    fee_adjusted_rr,
    position_size,
)


def test_v1_worked_example_low_confidence_wide_sl():
    """v1 vault: 60% conf + 3.0% SL -> ~2.1-2.2x при max 10."""
    assert dynamic_leverage(10, 60, 3.0) == pytest.approx(2.2, abs=0.15)


def test_v1_worked_example_high_confidence_tight_sl():
    """v1 vault: 90% conf + 0.5% SL -> 8.6x при max 10."""
    assert dynamic_leverage(10, 90, 0.5) == pytest.approx(8.6, abs=0.05)


def test_leverage_never_exceeds_max():
    assert dynamic_leverage(5, 100, 0.1) <= 5
    assert dynamic_leverage(10, 100, 0.5) <= 10


def test_leverage_floor_is_one():
    assert dynamic_leverage(10, 0, 5.0) >= 1.0


def test_fee_adjusted_rr_eats_thin_tp():
    """TP, не покрывающий комиссии, дает R:R около нуля или ниже."""
    # entry 100, SL 99 (1%), TP 100.2 (0.2%): fee-буфер 2*0.08% = 0.16
    rr = fee_adjusted_rr(100.0, 99.0, 100.2)
    assert rr < 0.1


def test_fee_adjusted_rr_normal_setup():
    # entry 100, SL 99, TP 103: (3 - 0.16) / 1 = 2.84
    assert fee_adjusted_rr(100.0, 99.0, 103.0) == pytest.approx(2.84, abs=0.001)


def test_position_size_risk_based():
    """Потеря при SL-хите = ровно risk_pct от equity."""
    s = position_size(equity_usd=1000, risk_pct=1.0, entry=100.0, sl=98.0, leverage=5)
    assert s.risk_usd == pytest.approx(10.0)
    assert s.qty == pytest.approx(5.0)  # $10 / $2 дистанции
    loss_at_sl = s.qty * abs(100.0 - 98.0)
    assert loss_at_sl == pytest.approx(10.0)
    assert s.margin_usd == pytest.approx(100.0)  # 500 notional / 5x


def test_position_size_rejects_zero_sl_distance():
    with pytest.raises(ValueError):
        position_size(1000, 1.0, 100.0, 100.0, 5)
