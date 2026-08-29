"""Исходящий Telegram-канал oskard: карточки сетапов + алерты.

Только ИСХОДЯЩЕЕ (sendMessage/sendPhoto). Входящие (getUpdates, кнопки) —
монополия Hermes: два процесса не могут делить long-poll одного бота, а
отправка сообщений конфликтов не создает.

Кнопки: callback_data "oskar:approve:{decision_id}" / "oskar:reject:{id}" /
"oskar:snooze:{id}" — Hermes-скилл oskar-approvals маппит их в POST
127.0.0.1:8321/decisions/{id}/....
"""

import io
from dataclasses import dataclass
from typing import Any

import httpx

from oskar.contracts.schemas import RiskVerdict, SetupCandidate
from oskar.data.symbols import to_display

API = "https://api.telegram.org"


@dataclass(frozen=True)
class SentMessage:
    message_id: int
    ok: bool


class TelegramSender:
    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None):
        if not token or not chat_id:
            raise ValueError("telegram token and chat_id are required")
        self._base = f"{API}/bot{token}"
        self._chat = chat_id
        self._http = client or httpx.Client(timeout=15)

    def send_text(self, text: str) -> SentMessage:
        r = self._http.post(
            f"{self._base}/sendMessage",
            json={"chat_id": self._chat, "text": text, "parse_mode": "HTML"},
        )
        payload = r.json()
        return SentMessage(
            message_id=int(payload.get("result", {}).get("message_id", 0)),
            ok=bool(payload.get("ok")),
        )

    def send_setup_card(
        self,
        candidate: SetupCandidate,
        verdict: RiskVerdict,
        chart_png: bytes | None,
        ttl_minutes: int,
    ) -> SentMessage:
        text = render_card_text(candidate, verdict, ttl_minutes)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": f"oskar:approve:{candidate.id}"},
                    {"text": "Reject", "callback_data": f"oskar:reject:{candidate.id}"},
                ],
                [{"text": "Snooze 15m", "callback_data": f"oskar:snooze:{candidate.id}"}],
            ]
        }
        if chart_png is not None:
            r = self._http.post(
                f"{self._base}/sendPhoto",
                data={
                    "chat_id": self._chat,
                    "caption": text,
                    "parse_mode": "HTML",
                    "reply_markup": _json(keyboard),
                },
                files={"photo": ("chart.png", chart_png, "image/png")},
            )
        else:
            r = self._http.post(
                f"{self._base}/sendMessage",
                json={
                    "chat_id": self._chat,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": keyboard,
                },
            )
        payload = r.json()
        return SentMessage(
            message_id=int(payload.get("result", {}).get("message_id", 0)),
            ok=bool(payload.get("ok")),
        )


def _json(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def render_card_text(candidate: SetupCandidate, verdict: RiskVerdict, ttl_minutes: int) -> str:
    """Компактная карточка: вся суть решения в одном экране телефона."""
    c = verdict.computed
    direction = "SHORT" if candidate.direction == "short" else "LONG"
    entry = next(o for o in candidate.entry_options if o.kind == "market")
    sl = candidate.sl_options[0]
    tp = candidate.tp_options[0]
    sl_pct = abs(sl.price - entry.price) / entry.price * 100
    lines = [
        f"<b>{to_display(candidate.symbol)} {direction}</b> | {candidate.setup_type} {candidate.timeframe}",
        f"score {candidate.score}",
        f"entry {entry.price:.6g} | SL {sl.price:.6g} ({sl_pct:.1f}%) | TP {tp.price:.6g}",
    ]
    if c is not None:
        lines.append(
            f"R:R {c.rr_fee_adjusted:.2f} | risk ${c.risk_usd:.2f} | lev {c.leverage:g}x | qty {c.qty:.6g}"
        )
    f = candidate.features
    ctx = []
    if f.fvg is not None:
        ctx.append(f"FVG {f.fvg.bottom:.6g}-{f.fvg.top:.6g}")
    if f.ob is not None:
        ctx.append(f"OB {f.ob.bottom:.6g}-{f.ob.top:.6g}")
    if f.swept_liquidity:
        ctx.append("sweep")
    if ctx:
        lines.append(" | ".join(ctx))
    lines.append(f"trend {f.trend_mtf or '?'} | RSI {f.rsi or '?'} | TTL {ttl_minutes} min")
    return "\n".join(lines)


def render_chart(df, candidate: SetupCandidate) -> bytes:
    """PNG: свечи + зоны FVG/OB + линии entry/SL/TP (mplfinance)."""
    import matplotlib

    matplotlib.use("Agg")
    import mplfinance as mpf
    import pandas as pd

    plot_df = df.tail(120).copy()
    plot_df.index = pd.to_datetime(plot_df["ts"], unit="ms")
    plot_df = plot_df[["open", "high", "low", "close", "volume"]]
    plot_df.columns = ["Open", "High", "Low", "Close", "Volume"]

    entry = next(o for o in candidate.entry_options if o.kind == "market").price
    sl = candidate.sl_options[0].price
    tp = candidate.tp_options[0].price

    hlines = dict(
        hlines=[entry, sl, tp],
        colors=["gray", "red", "green"],
        linestyle="--",
        linewidths=1,
    )
    fig, axes = mpf.plot(
        plot_df,
        type="candle",
        style="charles",
        hlines=hlines,
        volume=False,
        returnfig=True,
        figsize=(10, 6),
        title=f"{to_display(candidate.symbol)} {candidate.timeframe}",
    )
    ax = axes[0]
    x0, x1 = ax.get_xlim()
    f = candidate.features
    if f.fvg is not None:
        ax.fill_between([x0, x1], f.fvg.bottom, f.fvg.top, alpha=0.15, color="purple")
    if f.ob is not None:
        ax.fill_between([x0, x1], f.ob.bottom, f.ob.top, alpha=0.15, color="orange")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    import matplotlib.pyplot as plt

    plt.close(fig)
    return buf.getvalue()
