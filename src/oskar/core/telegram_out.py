"""Исходящий Telegram-канал oskard: карточки сетапов + алерты.

Только ИСХОДЯЩЕЕ (sendMessage/sendPhoto). Входящие (getUpdates, кнопки) —
монополия Hermes: два процесса не могут делить long-poll одного бота, а
отправка сообщений конфликтов не создает.

Кнопки: callback_data "oskar:approve:{decision_id}" / "oskar:reject:{id}" /
"oskar:snooze:{id}" — Hermes-скилл oskar-approvals маппит их в POST
127.0.0.1:8321/decisions/{id}/....
"""

import html
import io
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from oskar.contracts.schemas import RiskVerdict, SetupCandidate
from oskar.data.symbols import to_display

API = "https://api.telegram.org"

logger = logging.getLogger("oskar.telegram")


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

    def remove_buttons(self, message_id: int | str) -> bool:
        """Снять клавиатуру с карточки (TTL истек): нажимать больше нечего."""
        r = self._http.post(
            f"{self._base}/editMessageReplyMarkup",
            json={
                "chat_id": self._chat, "message_id": int(message_id),
                "reply_markup": {"inline_keyboard": []},
            },
        )
        return bool(r.json().get("ok"))

    def send_text(self, text: str) -> SentMessage:
        """Алерт. Ответ Telegram проверяется (блок H, 2026-10-16): ok:false (HTML не разобрался —
        «&», «<» в тексте ошибки биржи) или HTTP/сетевая ошибка -> logger.error и повтор тем же
        текстом без parse_mode. Раньше ответ не смотрели: алерт «БЕЗ SL» мог молча не дойти."""
        sent, why = self._post_text(text, html_mode=True)
        if sent.ok:
            return sent
        logger.error("telegram alert refused (%s) - resending as plain text", why)
        sent, why = self._post_text(text, html_mode=False)
        if not sent.ok:
            logger.error("telegram alert NOT delivered (%s): %s", why, text[:200])
        return sent

    def _post_text(self, text: str, *, html_mode: bool) -> tuple[SentMessage, str]:
        body = {"chat_id": self._chat, "text": text}
        if html_mode:
            body["parse_mode"] = "HTML"
        try:
            r = self._http.post(f"{self._base}/sendMessage", json=body)
            payload = r.json()
            if r.status_code >= 400 or not payload.get("ok"):
                why = str(payload.get("description", ""))[:200]
                return SentMessage(0, False), f"HTTP {r.status_code}: {why}"
            return SentMessage(int((payload.get("result") or {}).get("message_id", 0)), True), ""
        except Exception as e:  # noqa: BLE001 - сеть/не-JSON; в URL токен — пишем только тип
            return SentMessage(0, False), type(e).__name__

    def send_setup_card(
        self,
        candidate: SetupCandidate,
        verdict: RiskVerdict,
        chart_png: bytes | None,
        ttl_minutes: int,
        scout_verdict=None,
        plan=None,
        note: str | None = None,  # строка-итог (автомат: «исполнено ...» / «не исполнил ...»)
        buttons: bool = True,  # автомат исполнил — нажимать нечего
    ) -> SentMessage:
        text = render_card_text(candidate, verdict, ttl_minutes, scout_verdict, plan)
        if note:
            text += "\n\n<b>" + html.escape(note[:300]) + "</b>"
        keyboard = {"inline_keyboard": build_keyboard(candidate.id, plan) if buttons else []}
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


def _clip(text: str, limit: int = 220) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_keyboard(decision_id: str, plan=None) -> list[list[dict]]:
    """Кнопки карточки: одно нажатие = система делает всё сама.

    ⚡ Маркет — вход сейчас; ⏳ Лимит <цена> — отложенный ордер по найденному
    уровню (FVG/OB). Обе ставят SL и TP тем же действием.
    callback_data <= 64 байт (лимит Telegram): 'oskar:market:<id>'.
    """
    rows: list[list[dict]] = [[
        {"text": "⚡ Маркет", "callback_data": f"oskar:market:{decision_id}"[:64]},
    ]]
    if plan is not None and getattr(plan, "entry_limit", None):
        rows[0].append({
            "text": f"⏳ Лимит {plan.entry_limit:.6g}",
            "callback_data": f"oskar:limit:{decision_id}"[:64],
        })
    rows.append([
        {"text": "❌ Пропустить", "callback_data": f"oskar:reject:{decision_id}"[:64]},
        {"text": "💤 15 мин", "callback_data": f"oskar:snooze:{decision_id}"[:64]},
    ])
    return rows


def _card_levels(candidate: SetupCandidate, plan=None) -> tuple[float, float, float]:
    """Цены карточки — из ИСПОЛНЯЕМОГО плана, если он есть: Trader мог выбрать
    не первые SL/TP из меню, а оператор должен видеть то, что реально уйдет на
    биржу (ревью Codex 2026-10-13). Без плана — первые опции кандидата."""
    if plan is not None:
        return float(plan.entry_market), float(plan.sl), float(plan.tp)
    entry = next(o for o in candidate.entry_options if o.kind == "market")
    return entry.price, candidate.sl_options[0].price, candidate.tp_options[0].price


def render_card_text(
    candidate: SetupCandidate,
    verdict: RiskVerdict,
    ttl_minutes: int,
    scout_verdict=None,
    plan=None,
) -> str:
    """Карточка-выжимка (фидбек Ивана): решение за 5 секунд с телефона.

    Только то, что нужно для «да/нет»: направление, цены, риск, вывод одной
    фразой. Вся техника (зоны, индикаторы, полные дебаты) — в журнале,
    доступна через Hermes («почему этот сетап?»)."""
    c = verdict.computed
    arrow = "🔻" if candidate.direction == "short" else "🔺"
    direction = "SHORT" if candidate.direction == "short" else "LONG"
    entry_px, sl_px, tp_px = _card_levels(candidate, plan)
    sl_pct = abs(sl_px - entry_px) / entry_px * 100

    lines = [
        f"{arrow} <b>{html.escape(to_display(candidate.symbol))} {direction}</b> · {candidate.timeframe}",
        f"вход {entry_px:.6g} → стоп {sl_px:.6g} ({sl_pct:.1f}%) → цель {tp_px:.6g}",
    ]
    stats = []
    if c is not None:
        stats.append(f"R:R {c.rr_fee_adjusted:.1f}")
        stats.append(f"риск ${c.risk_usd:.2f}")
        stats.append(f"плечо {c.leverage:g}x")
    if scout_verdict is not None:
        stats.append(f"conf {scout_verdict.confidence}")
    if stats:
        lines.append(" · ".join(stats))
    lines.append(f"⏱ {ttl_minutes} мин")
    if scout_verdict is not None:
        tldr = getattr(scout_verdict, "tldr", "") or _clip(scout_verdict.bull_case, 140)
        lines.append("")
        lines.append(html.escape(_clip(tldr, 160)))  # текст модели в parse_mode=HTML — только экранированный
    return "\n".join(lines)


def render_chart(df, candidate: SetupCandidate, plan=None) -> bytes:
    """PNG: свечи + зоны FVG/OB + линии entry/SL/TP (mplfinance) — уровни плана."""
    import matplotlib

    matplotlib.use("Agg")
    import mplfinance as mpf
    import pandas as pd

    plot_df = df.tail(120).copy()
    plot_df.index = pd.to_datetime(plot_df["ts"], unit="ms")
    plot_df = plot_df[["open", "high", "low", "close", "volume"]]
    plot_df.columns = ["Open", "High", "Low", "Close", "Volume"]

    entry, sl, tp = _card_levels(candidate, plan)

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
