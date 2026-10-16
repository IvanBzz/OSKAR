"""Обработчик кнопок карточек — long-poll выделенного сигнального бота.

Почему отдельный бот: getUpdates основного бота принадлежит Hermes, а его
адаптер молча выбрасывает чужие callback_data (проверено по исходнику —
фиксированный список префиксов, наш oskar:* уходит в return). Сигнальный бот
принадлежит oskard целиком: карточки шлет он, нажатия ловит он, реакция
мгновенная (без LLM в цепочке).

Безопасность: реагируем ТОЛЬКО на нажатия из allowlist-чата; всё прочее
игнорируется без ответа.

Текстовые команды (ст. 6, ревью 2026-10-13): конституция обещает Telegram
`/panic`, а поллер запрашивал только callback_query — сообщение до демона не
доходило. Теперь тот же long-poll берет и `message`. От allowlist-автора в
allowlist-чате (та же проверка, что у кнопок) исполняются `/panic` — сразу,
первым в пачке — и `/status`; всё прочее молча игнорируется.

Блок I, 2026-10-16: `/digest` — сводка за 24 ч (ops/digest.py); каждая команда — INFO-строка
в логе (имя команды, без текста сообщения и токена).
"""

import html
import logging
import threading
import time

import httpx

from oskar.core.approvals import resolve_approval
from oskar.core.emergency import engage_panic
from oskar.core.state import RuntimeState
from oskar.journal.db import transaction
from oskar.risk.drawdown import read_halt

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
# message — для текстовых команд (/panic, /status); без него Telegram их не отдает
ALLOWED_UPDATES = '["callback_query","message"]'
PANIC_COMMAND = "panic"
STATUS_COMMAND = "status"
MODE_COMMAND = "mode"
DIGEST_COMMAND = "digest"
MODE_VERB = "mode"
KNOWN_COMMANDS = (PANIC_COMMAND, STATUS_COMMAND, MODE_COMMAND, DIGEST_COMMAND)
BOT_COMMANDS = [
    {"command": "mode", "description": "режим: автомат / с подтверждением"},
    {"command": "status", "description": "состояние OSKAR одной строкой"},
    {"command": "digest", "description": "сводка за 24 ч: воронка, сделки, P&L, инварианты"},
    {"command": "panic", "description": "EMERGENCY STOP: снять входы, закрыть позиции"},
]


def mode_message(current: str) -> tuple[str, dict]:
    """Сообщение-«ползунок» режима: текст + две кнопки, активная отмечена."""
    auto = current == "auto"
    text = ("Режим исполнения: <b>автомат</b> — сделки ставятся сами (VST), карточки приходят "
            "уведомлением." if auto else
            "Режим исполнения: <b>с подтверждением</b> — каждую сделку подтверждаешь кнопкой.")
    keyboard = {"inline_keyboard": [[
        {"text": ("● " if auto else "○ ") + "🤖 Автомат", "callback_data": "oskar:mode:auto"},
        {"text": ("○ " if auto else "● ") + "✋ С подтверждением",
         "callback_data": "oskar:mode:confirm"},
    ]]}
    return text, keyboard
STALE_COMMAND_SEC = 60  # /panic старше — ждал в очереди Telegram, пока демон лежал


def parse_command(text: str) -> str | None:
    """'/panic', '/panic@OskarSignalsBot', '/PANIC сейчас' -> 'panic'; не команда -> None."""
    words = (text or "").strip().split(maxsplit=1)
    if not words or not words[0].startswith("/"):
        return None
    return words[0][1:].split("@", 1)[0].lower() or None


def _esc(value, limit: int = 160) -> str:
    """Динамический текст (биржа, исключения, журнал) в HTML-сообщение: обрезка, затем экранирование."""
    text = str(value)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return html.escape(text)


def panic_reply(result: dict, waited_sec: float = 0.0) -> str:
    """Короткая сводка panic для Telegram (HTML): что снято, что закрыто, что упало."""
    lines = ["🚨 <b>PANIC</b>: торговля остановлена (safe_mode + пауза)."]
    exchange = result.get("exchange")
    if exchange is None:
        lines.append("Биржа НЕ тронута: монитор позиций не подключен (нет ключей биржи).")
    elif "error" in exchange:
        lines.append(f"Биржа: ОШИБКА — {_esc(exchange['error'])}. Проверь позиции на бирже вручную!")
    else:
        errors = exchange.get("errors") or []
        lines.append(
            f"Снято входов: {_esc(exchange.get('cancelled', 0))}, "
            f"закрыто позиций: {_esc(exchange.get('closed', 0))}, ошибок: {len(errors)}."
        )
        lines += [f"⚠️ {_esc(e)}" for e in errors[:3]]
        if len(errors) > 3:
            lines.append(f"…и еще {len(errors) - 3}")
        unclear = exchange.get("unclear") or []
        if unclear:  # ревью H3, 3-й круг: по symbol/side не закрываем — оператор проверяет сам
            lines.append(f"⚠️ входов с неясным исходом: {len(unclear)} ({_esc(', '.join(unclear[:3]))}) — "
                         "по symbol/side ничего не закрыто, проверь позиции вручную.")
    if result.get("persist_error"):
        lines.append(
            f"⚠️ флаг не записан в журнал ({_esc(result['persist_error'])}): "
            "после рестарта panic не восстановится."
        )
    if waited_sec >= STALE_COMMAND_SEC:
        lines.append(f"Команда ждала {int(waited_sec // 60)} мин — демон был недоступен.")
    lines.append("Снять — только явной командой оператора: oskar panic-reset.")
    return "\n".join(lines)


def status_line(state: RuntimeState, runner=None) -> str:
    """Одна строка для /status: режим, позиции, карточки, equity. Журнал — только чтение."""
    if state.panic_at is not None:
        mode = "PANIC с " + time.strftime("%H:%M UTC", time.gmtime(state.panic_at))
    elif state.safe_mode:
        mode = "SAFE MODE" + (", пауза" if state.paused else "")
    else:
        mode = "пауза" if state.paused else "работает"
    parts = [f"OSKAR {state.run_id}: {mode}"]
    try:
        live = dict(state.journal.execute(
            "SELECT status, COUNT(*) FROM trades WHERE status IN ('open','pending') GROUP BY status"
        ).fetchall())
        cards = state.journal.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE resolution IS NULL"
        ).fetchone()[0]
        parts.append(f"позиций {live.get('open', 0)}, лимиток {live.get('pending', 0)}, карточек {cards}")
        halt = read_halt(state.journal)  # блок H, 2026-10-16: защелка просадки видна в /status
        if halt is not None:
            parts.append(f"СТОП ПО ПРОСАДКЕ −{halt.pct:.1f}% от пика ${halt.peak:.2f}: "
                         "новые входы закрыты (oskar drawdown-reset)")
    except Exception as e:  # noqa: BLE001 - статус не падает из-за журнала
        parts.append(f"журнал недоступен ({type(e).__name__})")
    if runner is not None:
        parts.append(f"equity ${runner.equity_usd:.2f} ({runner.equity_source})")
    up = state.uptime_sec
    parts.append(f"аптайм {up // 3600}ч{up % 3600 // 60:02d}м")
    return " · ".join(parts)


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
        allowed_user_ids: set[str] | None = None,  # по умолчанию = chat id (личный чат Ивана)
        on_panic=None,  # () -> dict (ответ engage_panic); демон передает engage_panic(state, monitor)
        on_status=None,  # () -> str: одна строка для /status
        on_mode=None,  # (new_mode | None) -> текущий режим; None — переключателя нет
        on_digest=None,  # () -> str: сводка за 24 ч (HTML, уже экранирована); None — нет сводки
    ):
        if not token or not allowed_chat_id:
            raise ValueError("signals bot token and chat id are required")
        self._base = f"{API}/bot{token}"
        self._chat = str(allowed_chat_id)
        self._users = {str(u) for u in (allowed_user_ids or {allowed_chat_id})}
        self._state = state
        self._http = client or httpx.Client(timeout=35)
        self._on_resolution = on_resolution
        # /panic без подключенного обработчика все равно исполняется: флаги + meta;
        # биржу трогает только engage_panic с монитором (его передает демон)
        self._on_panic = on_panic or (lambda: engage_panic(state))
        self._on_status = on_status or (lambda: status_line(state))
        self._on_mode = on_mode
        self._on_digest = on_digest
        self._offset = 0
        self.not_ok_streak = 0
        self._stop = threading.Event()

    # ------------------------------------------------------------------ core

    def poll_once(self, timeout: int = 25) -> int:
        """Один цикл getUpdates. Возвращает число обработанных апдейтов (нажатия + сообщения)."""
        r = self._http.get(
            f"{self._base}/getUpdates",
            params={
                "timeout": timeout,
                "offset": self._offset,
                "allowed_updates": ALLOWED_UPDATES,
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
        # ст. 6: /panic из пачки — первым. Нажатие «Маркет» из той же пачки (типично после
        # простоя демона) иначе успело бы поставить ордер; после panic его отвергнет safe_mode.
        # sorted устойчив: остальные апдейты идут в порядке Telegram.
        updates = sorted(payload.get("result", []), key=lambda u: not self._is_panic(u))
        handled = 0
        for upd in updates:
            # offset — до обработки и для любого типа апдейта: упавший обработчик не крутит
            # апдейт по кругу, /panic не исполняется дважды
            self._offset = max(self._offset, upd["update_id"] + 1)
            try:
                if upd.get("callback_query"):
                    self._handle_callback(upd["callback_query"])
                elif upd.get("message"):
                    self._handle_message(upd["message"])
                else:
                    continue
                handled += 1
            except Exception as e:  # noqa: BLE001 - один апдейт не роняет пачку (в ней может быть /panic)
                logger.error("update %s failed: %s", upd.get("update_id"), e)
        return handled

    def _allowed(self, msg: dict) -> bool:
        """Allowlist сообщения: и чат, и автор (в группе писать может любой участник)."""
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        from_id = str((msg.get("from") or {}).get("id", ""))
        return chat_id == self._chat and from_id in self._users

    def _is_panic(self, upd: dict) -> bool:
        try:
            msg = upd.get("message")
            return bool(msg) and self._allowed(msg) and parse_command(msg.get("text") or "") == PANIC_COMMAND
        except Exception:  # noqa: BLE001 - кривой апдейт не должен заклинить сортировку всей пачки
            return False

    def _handle_message(self, msg: dict) -> None:
        if not self._allowed(msg):
            logger.warning(
                "message from foreign chat/user %s/%s ignored",
                (msg.get("chat") or {}).get("id"), (msg.get("from") or {}).get("id"),
            )
            return  # чужим не отвечаем
        command = parse_command(msg.get("text") or "")
        if command in KNOWN_COMMANDS:
            # блок I, 2026-10-16: команды видны в data/oskard.log (без текста сообщения и токена)
            logger.info("telegram command /%s (allowlisted chat)", command)
        if command == PANIC_COMMAND:
            self._reply(self._run_panic(msg))
        elif command == STATUS_COMMAND:
            self._reply(self._run_status())
        elif command == MODE_COMMAND:
            if self._on_mode is None:
                self._reply("переключатель режима не подключен (нет ключей биржи)")
            else:
                text, keyboard = mode_message(self._on_mode(None))
                self._reply(text, keyboard)
        elif command == DIGEST_COMMAND:
            self._reply(self._run_digest())
        # не команда / неизвестная команда — молча

    def _run_digest(self) -> str:
        if self._on_digest is None:
            return "сводка не подключена"
        try:
            return str(self._on_digest())
        except Exception as e:  # noqa: BLE001 - сводка не роняет поллер
            logger.error("digest failed: %s", e)
            return f"сводка недоступна: {_esc(e)}"

    def _run_panic(self, msg: dict) -> str:
        self._state.panic()  # флаги — сразу, даже если обработчик ниже сломан (ст. 6 п. 2)
        try:
            result = self._on_panic() or {}
        except Exception as e:  # noqa: BLE001 - флаги уже стоят; оператор должен узнать о сбое
            logger.critical("PANIC via Telegram: handler failed: %s", e)
            return (
                "🚨 <b>PANIC</b>: торговля остановлена (safe_mode + пауза), но снятие ордеров "
                f"упало: {_esc(e)}. Проверь биржу вручную!"
            )
        logger.warning("PANIC via Telegram: %s", result)
        waited = time.time() - float(msg.get("date") or time.time())
        return panic_reply(result, waited)

    def _run_status(self) -> str:
        try:
            line = str(self._on_status())
        except Exception as e:  # noqa: BLE001 - статус не роняет поллер
            line = f"статус недоступен: {e}"
        return _esc(line, 400)

    def _handle_callback(self, cb: dict) -> None:
        cb_id = cb.get("id", "")
        msg = cb.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        from_id = str((cb.get("from") or {}).get("id", ""))
        data = cb.get("data", "")

        # allowlist и по чату, и по нажавшему: в группе кнопку мог бы нажать любой участник
        if chat_id != self._chat or from_id not in self._users:
            logger.warning("callback from foreign chat/user %s/%s ignored", chat_id, from_id)
            return  # без answer — чужим не отвечаем

        parts = data.split(":", 2)
        known = len(parts) == 3 and parts[0] == "oskar" and (
            parts[1] in VERB_ACTIONS or parts[1] in (PING_VERB, MODE_VERB)
        )
        if not known:
            self._answer(cb_id, "не понял кнопку")
            return
        verb, decision_id = parts[1], parts[2]

        if verb == MODE_VERB:
            self._switch_mode(cb_id, msg, decision_id)
            return

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
        if verb in EXECUTING_VERBS and self._on_resolution is None:
            # исполнитель не подключен (нет ключей биржи): «Беру по рынку» было бы ложью
            answer = "исполнение выключено (нет ключей биржи) — approve записан, ордер НЕ поставлен"
            logger.warning("button %s %s: executor not wired, approve only", verb, decision_id)
        # market/limit: одно нажатие ставит вход + SL + TP
        if verb in EXECUTING_VERBS and self._on_resolution is not None:
            try:
                result = self._on_resolution(decision_id, EXECUTING_VERBS[verb])
                if result is not None:
                    answer = result.message if result.ok else f"не исполнено: {result.message}"
                    if not result.ok and getattr(result, "entry_sent", False):
                        # вход УЖЕ на бирже: решение остается approved, карточка закрывается
                        # с предупреждением — повторное нажатие дало бы вторую позицию (класс v1)
                        self._answer(cb_id, answer)
                        self._finalize_card(msg, answer)
                        logger.critical("execution degraded for %s: %s", decision_id, result.message)
                        return
                    if not result.ok:  # до биржи ничего не дошло: откат, карточка живая
                        with transaction(self._state.journal):
                            self._state.journal.execute(
                                "UPDATE pending_approvals SET resolution=NULL, resolved_at=NULL "
                                "WHERE decision_id=?", (decision_id,),
                            )
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

    def _switch_mode(self, cb_id: str, msg: dict, target: str) -> None:
        """Ползунок режима: переключить, ответить всплывашкой, перерисовать сообщение."""
        if self._on_mode is None:
            self._answer(cb_id, "переключатель не подключен")
            return
        try:
            current = self._on_mode(target)
        except Exception as e:  # noqa: BLE001 - например, автомат вне paper
            self._answer(cb_id, f"не переключил: {e}")
            return
        label = "автомат" if current == "auto" else "с подтверждением"
        self._answer(cb_id, f"режим: {label}")
        text, keyboard = mode_message(current)
        try:
            self._http.post(f"{self._base}/editMessageText", json={
                "chat_id": self._chat, "message_id": msg.get("message_id"), "text": text,
                "parse_mode": "HTML", "reply_markup": keyboard,
            })
        except Exception as e:  # noqa: BLE001 - косметика
            logger.warning("mode message edit failed: %s", e)
        logger.warning("execution mode switched via Telegram -> %s", current)

    def _reply(self, text: str, keyboard: dict | None = None) -> None:
        """Ответ на команду в allowlist-чат (HTML; динамика уже экранирована)."""
        body = {"chat_id": self._chat, "text": text, "parse_mode": "HTML"}
        if keyboard is not None:
            body["reply_markup"] = keyboard
        try:
            r = self._http.post(f"{self._base}/sendMessage", json=body)
            if not r.json().get("ok"):
                logger.warning("command reply refused: %s", str(r.json())[:200])
        except Exception as e:  # noqa: BLE001 - ответ вторичен, команда уже исполнена
            logger.warning("command reply failed: %s", e)

    def _finalize_card(self, msg: dict, status: str) -> None:
        """Убрать кнопки и дописать статус в подпись карточки."""
        try:
            message_id = msg.get("message_id")
            # Telegram возвращает подпись уже БЕЗ разметки: перед повторной отправкой
            # в parse_mode=HTML экранируем и ее, и статус (текст биржи/модели — не разметка)
            caption = msg.get("caption") or msg.get("text") or ""
            body = {
                "chat_id": self._chat,
                "message_id": message_id,
                "caption": f"{html.escape(caption)}\n\n<b>{html.escape(status[:300])}</b>",
                "parse_mode": "HTML",
            }
            endpoint = "editMessageCaption" if msg.get("caption") is not None else "editMessageText"
            if endpoint == "editMessageText":
                body["text"] = body.pop("caption")
            self._http.post(f"{self._base}/{endpoint}", json=body)
        except Exception as e:  # noqa: BLE001 - косметика, не логика
            logger.warning("card finalize failed: %s", e)

    # ---------------------------------------------------------------- thread

    def register_commands(self) -> None:
        """Меню команд сигнального бота (/mode /status /digest /panic) — подсказка в Telegram."""
        try:
            self._http.post(f"{self._base}/setMyCommands", json={"commands": BOT_COMMANDS})
        except Exception as e:  # noqa: BLE001 - меню вторично
            logger.warning("setMyCommands failed: %s", e)

    def run_forever(self) -> None:
        logger.info("button poller started (signals bot)")
        self.register_commands()
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
