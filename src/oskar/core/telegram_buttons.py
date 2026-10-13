"""Обработчик кнопок карточек — long-poll выделенного сигнального бота.

Почему отдельный бот: getUpdates основного бота принадлежит Hermes, а его
адаптер молча выбрасывает чужие callback_data (проверено по исходнику —
фиксированный список префиксов, наш oskar:* уходит в return). Сигнальный бот
принадлежит oskard целиком: карточки шлет он, нажатия ловит он, реакция
мгновенная (без LLM в цепочке).

Безопасность: реагируем ТОЛЬКО на нажатия из allowlist-чата; всё прочее
игнорируется без ответа.
"""

import logging
import threading

import httpx

from oskar.core.approvals import resolve_approval
from oskar.core.state import RuntimeState

logger = logging.getLogger("oskar.buttons")

API = "https://api.telegram.org"
# market/limit = approve + исполнение выбранным способом (одно нажатие делает всё)
EXECUTING_VERBS = {"market": "market", "limit": "limit"}
VERB_ACTIONS = {
    "market": "approve", "limit": "approve", "approve": "approve",
    "reject": "reject", "snooze": "snooze",
}
VERB_ANSWERS = {
    "market": "Беру по рынку ⚡", "limit": "Лимитка выставлена ⏳",
    "approve": "Взято ✅", "reject": "Пропущено ❌", "snooze": "Отложено 💤",
}
NOT_OK_BACKOFF_SEC = 30.0  # пауза после ответа ok=false (401 отозванный токен, 409, 5xx)
# Самопроверка цепочки Telegram -> поллер -> ответ: без решений, журнала и биржи.
PING_VERB = "ping"
PING_ANSWER = "Кнопки работают ✅"


def send_ping_card(token: str, chat_id: str, client: httpx.Client | None = None) -> bool:
    """Карточка с одной кнопкой; нажатие должно вернуть PING_ANSWER всплывашкой."""
    http = client or httpx.Client(timeout=15)
    r = http.post(
        f"{API}/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": "🔧 Самопроверка кнопок OSKAR. Нажми кнопку — должно всплыть «Кнопки работают ✅».",
            "reply_markup": {"inline_keyboard": [[
                {"text": "✅ Проверить", "callback_data": f"oskar:{PING_VERB}:selftest"},
            ]]},
        },
    )
    return bool(r.json().get("ok"))


class ButtonPoller:
    def __init__(
        self,
        token: str,
        allowed_chat_id: str,
        state: RuntimeState,
        client: httpx.Client | None = None,
        on_resolution=None,  # (decision_id, mode) -> ExecutionOutcome | None
    ):
        if not token or not allowed_chat_id:
            raise ValueError("signals bot token and chat id are required")
        self._base = f"{API}/bot{token}"
        self._chat = str(allowed_chat_id)
        self._state = state
        self._http = client or httpx.Client(timeout=35)
        self._on_resolution = on_resolution
        self._offset = 0
        self.not_ok_streak = 0
        self._stop = threading.Event()

    # ------------------------------------------------------------------ core

    def poll_once(self, timeout: int = 25) -> int:
        """Один цикл getUpdates. Возвращает число обработанных callback'ов."""
        r = self._http.get(
            f"{self._base}/getUpdates",
            params={
                "timeout": timeout,
                "offset": self._offset,
                "allowed_updates": '["callback_query"]',
            },
        )
        payload = r.json()
        if not payload.get("ok"):
            # 401 (токен отозван) / 409 / 5xx: без паузы это был tight-loop —
            # 3 запроса за 150 мс и лог на мегабайты (поймано 2026-10-12)
            self.not_ok_streak += 1
            if self.not_ok_streak == 1 or self.not_ok_streak % 60 == 0:
                logger.warning("getUpdates not ok (x%d): %s", self.not_ok_streak, str(payload)[:200])
            return -1
        self.not_ok_streak = 0
        handled = 0
        for upd in payload.get("result", []):
            self._offset = max(self._offset, upd["update_id"] + 1)
            cb = upd.get("callback_query")
            if cb:
                self._handle_callback(cb)
                handled += 1
        return handled

    def _handle_callback(self, cb: dict) -> None:
        cb_id = cb.get("id", "")
        msg = cb.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        data = cb.get("data", "")

        if chat_id != self._chat:
            logger.warning("callback from foreign chat %s ignored", chat_id)
            return  # без answer — чужим не отвечаем

        parts = data.split(":", 2)
        known = len(parts) == 3 and parts[0] == "oskar" and (
            parts[1] in VERB_ACTIONS or parts[1] == PING_VERB
        )
        if not known:
            self._answer(cb_id, "не понял кнопку")
            return
        verb, decision_id = parts[1], parts[2]

        if verb == PING_VERB:
            self._answer(cb_id, PING_ANSWER)
            self._finalize_card(msg, "кнопки проверены ✅")
            logger.info("button ping ok (%s)", decision_id)
            return

        out = resolve_approval(
            self._state.journal, decision_id, VERB_ACTIONS[verb],
            paused=self._state.paused, safe_mode=self._state.safe_mode,
        )
        if not out.ok:
            self._answer(cb_id, out.message)
            logger.info("button %s %s rejected: %s", verb, decision_id, out.message)
            return

        answer = VERB_ANSWERS[verb]
        # market/limit: одно нажатие ставит вход + SL + TP
        if verb in EXECUTING_VERBS and self._on_resolution is not None:
            try:
                result = self._on_resolution(decision_id, EXECUTING_VERBS[verb])
                if result is not None:
                    answer = result.message if result.ok else f"не исполнено: {result.message}"
                    if not result.ok:  # откат решения: карточка остается живой
                        self._state.journal.execute(
                            "UPDATE pending_approvals SET resolution=NULL, resolved_at=NULL "
                            "WHERE decision_id=?", (decision_id,),
                        )
                        self._state.journal.commit()
                        self._answer(cb_id, answer)
                        logger.error("execution failed for %s: %s", decision_id, result.message)
                        return
            except Exception as e:  # noqa: BLE001 - исполнение не роняет поллер
                logger.error("on_resolution failed for %s: %s", decision_id, e)
                answer = f"ошибка исполнения: {e}"

        self._answer(cb_id, answer)
        if verb != "snooze":
            self._finalize_card(msg, answer)
        logger.info("button %s %s -> %s", verb, decision_id, answer)

    # --------------------------------------------------------------- helpers

    def _answer(self, callback_id: str, text: str) -> None:
        try:
            self._http.post(
                f"{self._base}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text[:190]},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("answerCallbackQuery failed: %s", e)

    def _finalize_card(self, msg: dict, status: str) -> None:
        """Убрать кнопки и дописать статус в подпись карточки."""
        try:
            message_id = msg.get("message_id")
            caption = msg.get("caption") or msg.get("text") or ""
            body = {
                "chat_id": self._chat,
                "message_id": message_id,
                "caption": f"{caption}\n\n<b>{status}</b>",
                "parse_mode": "HTML",
            }
            endpoint = "editMessageCaption" if msg.get("caption") is not None else "editMessageText"
            if endpoint == "editMessageText":
                body["text"] = body.pop("caption")
            self._http.post(f"{self._base}/{endpoint}", json=body)
        except Exception as e:  # noqa: BLE001 - косметика, не логика
            logger.warning("card finalize failed: %s", e)

    # ---------------------------------------------------------------- thread

    def run_forever(self) -> None:
        logger.info("button poller started (signals bot)")
        while not self._stop.is_set():
            try:
                if self.poll_once() < 0:
                    self._stop.wait(NOT_OK_BACKOFF_SEC)
            except Exception as e:  # noqa: BLE001 - сеть: подождать и продолжить
                logger.warning("poll failed: %s", e)
                self._stop.wait(5)

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="oskar-buttons", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
