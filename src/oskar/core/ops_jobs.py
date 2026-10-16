"""Фоновые ops-задачи демона (блок I, 2026-10-16): инварианты по расписанию и дневная сводка.

Инварианты (ст. 5 п. 4, часть «алерт»): `oskar invariants` гонялся только руками, красный
вердикт никто не видел. Теперь демон прогоняет ops/invariants.evaluate через 10 мин после
старта и далее раз в ops.invariants_every_hours (6 ч). Новый red -> Telegram-алерт с id,
описанием и detail; тот же red повторно — не чаще раза в 24 ч (отметки в meta — рестарт
супервизором не превращает алерт в спам). Вердикт перестал быть red — отметка снимается,
следующий red снова «новый». Заметку в vault демон не пишет (он не коммитит), авто-revert —
не делает (ст. 5 п. 4 «авто-revert» — отдельный шаг).

Сводка: раз в сутки в ops.digest_time по ops.timezone (21:00 Europe/Moscow), текст —
ops/digest.build_digest. Отметка «за этот день отправлено» в meta: рестарт в 21:00:30 не шлет
вторую, а рестарт в 21:10 досылает пропущенную (окно DIGEST_GRACE).

Поток один, тик раз в TICK_SEC — по образцу ShadowRunner/PositionMonitor (Event.wait, stop()).
"""

import html
import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone, tzinfo

from oskar.journal.db import transaction

logger = logging.getLogger("oskar.ops")

TICK_SEC = 30.0
INVARIANTS_FIRST_DELAY = timedelta(minutes=10)
DEFAULT_INVARIANTS_EVERY_HOURS = 6.0
RED_REPEAT = timedelta(hours=24)
DIGEST_GRACE = timedelta(hours=2)  # рестарт после времени сводки — дослать, но не утром
DEFAULT_DIGEST_TIME = "21:00"
DEFAULT_TIMEZONE = "Europe/Moscow"
RED_ALERTS_KEY = "invariant_red_alerted"  # meta: {"INV-002": "<iso последнего алерта>"}
DIGEST_SENT_KEY = "digest_last_sent"  # meta: локальная дата последней сводки


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_iso(s) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s)[:-1] if str(s).endswith("Z") else str(s))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def load_tz(name: str) -> tzinfo:
    """ZoneInfo; без базы часовых поясов (Windows без tzdata) Москва — фиксированный UTC+3
    (без перехода на летнее время с 2014), прочее — ошибка конфига."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as e:  # noqa: BLE001
        if name == DEFAULT_TIMEZONE:
            return timezone(timedelta(hours=3), "MSK")
        raise ValueError(f"unknown timezone {name!r}: {e}") from e


@dataclass(frozen=True)
class OpsConfig:
    digest_at: time
    tz: tzinfo
    tz_name: str
    invariants_every: timedelta

    @property
    def tz_label(self) -> str:
        return "МСК" if self.tz_name == DEFAULT_TIMEZONE else self.tz_name


def ops_config(settings: dict) -> OpsConfig:
    """settings.ops; кривое значение — ValueError (демон отказывается стартовать)."""
    ops = settings.get("ops") or {}
    value = ops.get("digest_time", DEFAULT_DIGEST_TIME)
    if isinstance(value, int) and not isinstance(value, bool):
        # YAML 1.1: 21:00 без кавычек читается как 1260 (шестидесятеричное) — это минуты
        value = f"{value // 60}:{value % 60:02d}"
    raw = str(value)
    try:
        hh, mm = raw.split(":")
        at = time(int(hh), int(mm))
    except ValueError as e:
        raise ValueError(f"ops.digest_time={raw!r}: expected HH:MM") from e
    tz_name = str(ops.get("timezone", DEFAULT_TIMEZONE))
    hours = float(ops.get("invariants_every_hours", DEFAULT_INVARIANTS_EVERY_HOURS))
    if not hours > 0:
        raise ValueError(f"ops.invariants_every_hours={hours!r}: must be > 0")
    return OpsConfig(at, load_tz(tz_name), tz_name, timedelta(hours=hours))


def _meta_get(journal, key: str) -> str | None:
    row = journal.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(journal, key: str, value: str) -> None:
    with transaction(journal):
        journal.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


# -------------------------------------------------------------- инварианты


def render_red_alert(v) -> str:
    """Алерт по красному вердикту (HTML, динамика экранирована)."""
    value = "n/a" if v.value is None else (f"{v.value:g}" if isinstance(v.value, float) else str(v.value))
    lines = [
        f"🔴 <b>Инвариант {html.escape(v.id, quote=False)} красный</b>: "
        f"{html.escape(v.description or v.metric, quote=False)}",
        html.escape(f"{v.metric} = {value} (ожидание {v.expect}; окно {v.window}; n={v.n})", quote=False),
        html.escape(str(v.detail)[:600], quote=False),
        "Ст. 5 п. 4: проверь изменение, давшее красный; авто-revert не выполняется.",
    ]
    return "\n".join(lines)


class InvariantWatch:
    """evaluate по журналу + дедуп алертов red (не чаще RED_REPEAT на id, отметки в meta)."""

    def __init__(self, journal, notify, *, evaluate_fn=None, max_loss_hold_seconds: int | None = None):
        self._j = journal
        self._notify = notify
        self._max_hold = max_loss_hold_seconds
        self._evaluate = evaluate_fn or self._default_evaluate
        self.last_verdicts: list | None = None
        self.last_at: datetime | None = None

    def _default_evaluate(self, now: datetime):
        from oskar.ops.invariants import evaluate

        return evaluate(self._j, now, max_loss_hold_seconds=self._max_hold)

    def _alerted(self) -> dict[str, str]:
        try:
            data = json.loads(_meta_get(self._j, RED_ALERTS_KEY) or "{}")
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def check(self, now: datetime) -> list[str]:
        """Прогон; возвращает id, по которым ушел алерт."""
        verdicts = self._evaluate(now)
        self.last_verdicts, self.last_at = verdicts, now
        alerted = self._alerted()
        reds = {v.id: v for v in verdicts if v.status == "red"}
        sent: list[str] = []
        for vid, v in reds.items():
            last = _parse_iso(alerted.get(vid)) if vid in alerted else None
            if last is not None and now - last < RED_REPEAT:
                continue  # тот же red уже сообщен — не чаще раза в сутки
            logger.error("invariant %s RED: %s", vid, v.detail)
            ok = True
            if self._notify is not None:
                try:
                    res = self._notify(render_red_alert(v))
                    ok = getattr(res, "ok", True) is not False
                except Exception as e:  # noqa: BLE001 - не доставлен: попробуем следующим прогоном
                    logger.warning("invariant alert not sent: %s", e)
                    ok = False
            if ok:
                alerted[vid] = _iso(now)
                sent.append(vid)
        for v in verdicts:  # ушел из red — следующий red снова «новый»
            if v.status == "error":
                logger.warning("invariant %s not checkable: %s", v.id, v.detail)
        alerted = {k: t for k, t in alerted.items() if k in reds}
        _meta_set(self._j, RED_ALERTS_KEY, json.dumps(alerted, ensure_ascii=False))
        logger.info("invariants checked: %s", ", ".join(f"{v.id}={v.status}" for v in verdicts) or "none")
        return sent


# ------------------------------------------------------------------ поток


class OpsScheduler:
    def __init__(
        self,
        journal,
        cfg: OpsConfig,
        *,
        watch: InvariantWatch | None,
        digest_fn=None,  # () -> str: текст сводки (HTML)
        notify=None,  # (text) -> SentMessage | None
        started_at: datetime | None = None,
        now_fn=lambda: datetime.now(UTC),
    ):
        self._j = journal
        self._cfg = cfg
        self._watch = watch
        self._digest_fn = digest_fn
        self._notify = notify
        self._now = now_fn
        start = started_at or now_fn()
        self._next_invariants = start + INVARIANTS_FIRST_DELAY
        self._stop = threading.Event()

    # ---------------------------------------------------------------- digest

    def _digest_slot(self, now: datetime) -> tuple[str, datetime]:
        """(локальная дата, момент сводки этой даты в UTC)."""
        local = now.astimezone(self._cfg.tz)
        at = datetime.combine(local.date(), self._cfg.digest_at, tzinfo=self._cfg.tz)
        return local.date().isoformat(), at.astimezone(UTC)

    def digest_due(self, now: datetime) -> bool:
        day, at = self._digest_slot(now)
        if now < at or now - at > DIGEST_GRACE:
            return False
        return _meta_get(self._j, DIGEST_SENT_KEY) != day

    def send_digest(self, now: datetime) -> bool:
        day, _ = self._digest_slot(now)
        text = self._digest_fn()
        res = self._notify(text) if self._notify is not None else None
        ok = res is not None and getattr(res, "ok", True) is not False
        if ok:
            _meta_set(self._j, DIGEST_SENT_KEY, day)
            logger.info("daily digest sent (%s)", day)
        else:
            logger.warning("daily digest NOT sent (%s) - retry next tick", day)
        return ok

    # ------------------------------------------------------------------ tick

    def tick(self, now: datetime | None = None) -> None:
        now = now or self._now()
        if self._watch is not None and now >= self._next_invariants:
            self._next_invariants = now + self._cfg.invariants_every
            try:
                self._watch.check(now)
            except Exception as e:  # noqa: BLE001 - сбой проверки не роняет поток
                logger.error("invariants check failed: %s", e)
        if self._digest_fn is not None and self._notify is not None:
            try:
                if self.digest_due(now):
                    self.send_digest(now)
            except Exception as e:  # noqa: BLE001
                logger.error("daily digest failed: %s", e)

    def run_forever(self) -> None:
        logger.info("ops scheduler started (invariants every %s, digest %s %s)",
                    self._cfg.invariants_every, self._cfg.digest_at.strftime("%H:%M"), self._cfg.tz_name)
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(TICK_SEC)

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="oskar-ops", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
