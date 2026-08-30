"""Telegram-карточки: payload кнопок, текст, рендер графика (без сети)."""

from datetime import UTC, datetime

import httpx
import numpy as np
import pandas as pd
import pytest

from oskar.contracts.schemas import (
    FvgInfo,
    PriceOption,
    RiskComputed,
    RiskStep,
    RiskVerdict,
    SetupCandidate,
    SetupFeatures,
)
from oskar.core.telegram_out import (
    TelegramSender,
    render_card_text,
    render_chart,
)


def candidate() -> SetupCandidate:
    return SetupCandidate(
        id="SOLUSDTUSDT-100-short",
        ts=datetime.now(UTC),
        symbol="SOL/USDT:USDT",
        direction="short",
        setup_type="confluence",
        timeframe="15m",
        score=82,
        features=SetupFeatures(
            rsi=58.0, trend_mtf="bearish", swept_liquidity=True,
            fvg=FvgInfo(top=102.0, bottom=101.0, mitigated=False, timeframe="15m"),
        ),
        entry_options=[PriceOption(id="e-market", kind="market", price=100.0)],
        sl_options=[PriceOption(id="s-swing", kind="swing_high", price=103.65)],
        tp_options=[PriceOption(id="t-liq", kind="liquidity_pool", price=95.0)],
    )


def verdict() -> RiskVerdict:
    return RiskVerdict(
        decision_id="SOLUSDTUSDT-100-short",
        ts=datetime.now(UTC),
        passed=True,
        steps=[RiskStep(name="ok", passed=True)],
        computed=RiskComputed(qty=2.74, leverage=5.4, risk_usd=10.0, rr_fee_adjusted=1.33),
        caps_hash="x" * 64,
    )


class CaptureTransport(httpx.BaseTransport):
    def __init__(self):
        self.requests = []

    def handle_request(self, request):
        request.read()  # multipart идет стримом — прочитать до доступа к content
        self.requests.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 42}}
        )


def make_sender():
    transport = CaptureTransport()
    client = httpx.Client(transport=transport)
    return TelegramSender("TOKEN", "12345", client), transport


def test_send_text():
    sender, tr = make_sender()
    res = sender.send_text("hello")
    assert res.ok and res.message_id == 42
    assert tr.requests[0].url.path.endswith("/sendMessage")


def test_card_buttons_callback_data():
    sender, tr = make_sender()
    sender.send_setup_card(candidate(), verdict(), chart_png=None, ttl_minutes=20)
    import json

    body = json.loads(tr.requests[0].content)
    buttons = body["reply_markup"]["inline_keyboard"]
    flat = [b["callback_data"] for row in buttons for b in row]
    assert flat == [
        "oskar:approve:SOLUSDTUSDT-100-short",
        "oskar:reject:SOLUSDTUSDT-100-short",
        "oskar:snooze:SOLUSDTUSDT-100-short",
    ]


def test_card_with_chart_uses_sendphoto():
    sender, tr = make_sender()
    sender.send_setup_card(candidate(), verdict(), chart_png=b"\x89PNG fake", ttl_minutes=20)
    req = tr.requests[0]
    assert req.url.path.endswith("/sendPhoto")
    assert b"chart.png" in req.content


def test_card_text_is_minimal_digest():
    """Фидбек Ивана: только выжимка — направление, цены, риск, tldr.
    Никаких индикаторов, зон и score в карточке."""
    from oskar.contracts.schemas import SetupVerdict

    sv = SetupVerdict(
        candidate_id="SOLUSDTUSDT-100-short",
        tldr="Свип хая и ретест FVG сверху; цель — ликвидность под лоями.",
        bull_case="б" * 40, bear_case="м" * 40, bear_rebuttal="о" * 40,
        verdict="take", confidence=62,
    )
    text = render_card_text(candidate(), verdict(), ttl_minutes=20, scout_verdict=sv)
    assert "SOL/USDT SHORT" in text
    assert "вход 100" in text and "стоп 103.65 (3.7%)" in text and "цель 95" in text
    assert "риск $10.00" in text and "плечо 5.4x" in text and "conf 62" in text
    assert "Свип хая и ретест FVG" in text
    # техника скрыта:
    for banned in ("score", "FVG 101", "OB ", "RSI", "trend", "sweep", "confluence"):
        assert banned not in text
    # компактность: 6 строк, влезает в подпись фото с запасом
    assert len(text) < 400 and text.count("\n") <= 6


def test_sender_requires_credentials():
    with pytest.raises(ValueError):
        TelegramSender("", "123")
    with pytest.raises(ValueError):
        TelegramSender("tok", "")


def test_render_chart_produces_png():
    n = 150
    rng = np.random.default_rng(5)
    close = 100 + rng.normal(0, 0.5, n).cumsum()
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    df = pd.DataFrame({
        "ts": 1_700_000_000_000 + np.arange(n) * 900_000,
        "open": open_, "high": np.maximum(open_, close) + 0.3,
        "low": np.minimum(open_, close) - 0.3, "close": close,
        "volume": np.full(n, 1000.0),
    })
    png = render_chart(df, candidate())
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 10_000
