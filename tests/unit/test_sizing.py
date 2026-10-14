"""Сайзинг: формулы v1 против воркед-примеров из vault (dynamic-leverage.md) +
издержки стоп-аута в риск-капе и целое плечо (ревью 2026-10-13, Codex #20/#21)."""

import pytest

from oskar.risk.sizing import (
    ROUND_TRIP_FEE_PCT,
    TAKER_FEE_PCT,
    dynamic_leverage,
    fee_adjusted_rr,
    position_size,
    qty_for_risk,
    raw_leverage,
    stop_loss_per_unit,
)

TAKER = 0.0005  # BingX takerFeeRate (markets info), за сторону
SLIP = 0.0005  # исполнение стопа хуже SL на 0.05% от entry


def worst_stop_loss(qty: float, entry: float, sl: float) -> float:
    """Независимый пересчет потери на стопе: ход до исполнения + taker на обеих ногах."""
    exit_px = sl - SLIP * entry if sl < entry else sl + SLIP * entry
    return qty * abs(entry - exit_px) + qty * TAKER * (entry + exit_px)


def test_v1_worked_example_low_confidence_wide_sl():
    """v1 vault: 60% conf + 3.0% SL -> ~2.1-2.2x при max 10; на биржу уходит 2x."""
    assert raw_leverage(10, 60, 3.0) == pytest.approx(2.2, abs=0.15)
    assert dynamic_leverage(10, 60, 3.0) == 2


def test_v1_worked_example_high_confidence_tight_sl():
    """v1 vault: 90% conf + 0.5% SL -> 8.6x при max 10; на биржу уходит 8x."""
    assert raw_leverage(10, 90, 0.5) == pytest.approx(8.6, abs=0.05)
    assert dynamic_leverage(10, 90, 0.5) == 8


def test_leverage_is_whole_number_codex_scenario():
    """Codex #20: профиль 8x + conf 68 давали 4.4x, BingX принимает только целое —
    лестница проверяла маржу не на том плече, что уходило на биржу."""
    assert raw_leverage(8, 68, 1.5) == pytest.approx(4.416)
    lev = dynamic_leverage(8, 68, 1.5)
    assert lev == 4 and isinstance(lev, int)


def test_leverage_floor_survives_float_noise():
    """5 * conf(80) * vol(2.7%) = ровно 2, во float 1.9999999999999996 —
    floor без допуска срезал бы плечо до 1x."""
    assert raw_leverage(5, 80, 2.7) < 2.0
    assert dynamic_leverage(5, 80, 2.7) == 2


def test_leverage_never_exceeds_max():
    assert dynamic_leverage(5, 100, 0.1) <= 5
    assert dynamic_leverage(10, 100, 0.5) <= 10
    assert dynamic_leverage(7.5, 100, 0.5) == 7  # дробный кап — вниз, не вверх


def test_leverage_floor_is_one():
    assert dynamic_leverage(10, 0, 5.0) == 1


def test_round_trip_is_two_taker_legs():
    """Одно определение комиссии для R:R и сайзинга: круг = 2 ноги taker BingX.
    Было 0.08 «за круг» и еще x2 в R:R — реальный круг (0.10%) был занижен."""
    assert TAKER_FEE_PCT / 100.0 == pytest.approx(TAKER)
    assert ROUND_TRIP_FEE_PCT == pytest.approx(2 * TAKER_FEE_PCT)


def test_fee_adjusted_rr_eats_thin_tp():
    """TP, не покрывающий комиссии, дает R:R около нуля или ниже."""
    # entry 100, SL 99 (1%), TP 100.2 (0.2%): fee-буфер 2 x круг 0.10% = 0.20
    rr = fee_adjusted_rr(100.0, 99.0, 100.2)
    assert rr < 0.1


def test_fee_adjusted_rr_normal_setup():
    # entry 100, SL 99, TP 103: (3 - 2 * 0.10) / 1 = 2.80 — «2x round-trip fee» caps.yaml
    assert fee_adjusted_rr(100.0, 99.0, 103.0) == pytest.approx(2.80, abs=0.001)


def test_stop_loss_per_unit_components():
    # long: SL 99 -> исполнение 98.95; комиссии каждой ноги от своей цены
    assert stop_loss_per_unit(100.0, 99.0) == pytest.approx(1.05 + TAKER * (100.0 + 98.95))
    # short: SL 101 -> исполнение 101.05
    assert stop_loss_per_unit(100.0, 101.0) == pytest.approx(1.05 + TAKER * (100.0 + 101.05))


def test_position_size_risk_based():
    """Потеря при стоп-ауте С издержками = ровно risk_pct от equity."""
    s = position_size(equity_usd=1000, risk_pct=1.0, entry=100.0, sl=98.0, leverage=5)
    assert s.risk_usd == pytest.approx(10.0)
    assert worst_stop_loss(s.qty, 100.0, 98.0) == pytest.approx(10.0)
    # $10 / ($2 хода + $0.05 проскальзывания + комиссии) — меньше «голых» $10 / $2 = 5
    assert s.qty == pytest.approx(10.0 / (2.05 + TAKER * (100.0 + 97.95)))
    assert s.qty < 5.0
    assert s.leverage == 5
    assert s.margin_usd == pytest.approx(s.qty * 100.0 / 5)


def test_position_size_codex_scenario_costs_inside_cap():
    """Codex #21: $1000, 1%, entry 100, SL 99.5 -> было qty 20: на стопе $10 хода +
    ~$3 комиссий и проскальзывания. Теперь вся потеря на стопе = $10."""
    assert worst_stop_loss(10.0 / 0.5, 100.0, 99.5) > 12.5  # старый сайзинг: кап +30%
    s = position_size(1000, 1.0, 100.0, 99.5, 5)
    assert worst_stop_loss(s.qty, 100.0, 99.5) == pytest.approx(10.0)
    assert s.qty == pytest.approx(15.391, abs=1e-3)


def test_short_stop_fees_from_exit_price_not_entry():
    """Short: исполнение стопа дороже входа — оценка «круг от entry» дала бы qty
    больше и потерю выше капа. Считаем комиссию выхода от цены выхода."""
    s = position_size(1000, 1.0, 100.0, 100.5, 5)
    assert worst_stop_loss(s.qty, 100.0, 100.5) == pytest.approx(10.0)
    naive_qty = 10.0 / (0.5 + SLIP * 100.0 + 2 * TAKER * 100.0)
    assert naive_qty > s.qty
    assert worst_stop_loss(naive_qty, 100.0, 100.5) > 10.0


def test_qty_for_risk_resize_keeps_costs():
    """Пересайзинг под другую цену входа (кнопка «Маркет») обязан нести издержки:
    risk / |entry - SL| снова пробивает кап на комиссии и проскальзывание."""
    q = qty_for_risk(10.0, 102.0, 99.0)
    assert worst_stop_loss(q, 102.0, 99.0) == pytest.approx(10.0)
    assert worst_stop_loss(10.0 / 3.0, 102.0, 99.0) > 10.0


def test_position_size_rejects_fractional_leverage():
    """Дробное плечо биржа не примет: маржа на 4.4x — проверка не того, что уйдет."""
    with pytest.raises(ValueError):
        position_size(1000, 1.0, 100.0, 98.0, 4.4)
    with pytest.raises(ValueError):
        position_size(1000, 1.0, 100.0, 98.0, 0)


def test_position_size_rejects_zero_sl_distance():
    with pytest.raises(ValueError):
        position_size(1000, 1.0, 100.0, 100.0, 5)
