"""Резолвер подтверждений — единая логика для API (Hermes) и кнопок (poller).

Правила (ст. 6, TTL fail-closed): истекшее не воскрешается и помечается
expired; повторное решение отвергается; approve блокируется при pause/safe_mode
(reject разрешен всегда); snooze продлевает TTL живой карточки, но не дольше
MAX_CARD_LIFETIME_MIN от создания (план стареет — цена уходит).

Атомарность: решение фиксируется условным UPDATE ... WHERE resolution IS NULL
под замком журнала — гонка «кнопка + API одновременно» дает ровно одно approve.
Исход решения зеркалится в decisions.outcome (approval_rejected / approval_expired),
чтобы когорты counterfactuals не теряли отклоненные руками сетапы.

approve_and_execute (блок I, 2026-10-16): approve через API (Hermes) только помечал решение
approved и отвечал «взято в работу», а исполнял его никто — карточка потом отвечала 409,
сетап терялся. Теперь API зовет тот же охраняемый исполнитель, что кнопки и автомат
(equity_guarded(executor.execute)), и отвечает фактическим исходом.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from oskar.journal.db import transaction

logger = logging.getLogger("oskar.approvals")

SNOOZE_MINUTES = 15
MAX_CARD_LIFETIME_MIN = 60  # сколько карточка может жить с учетом всех snooze
ENTRY_TYPES = ("market", "limit")


@dataclass(frozen=True)
class ResolveOutcome:
    ok: bool
    code: int  # семантика HTTP: 200/404/409/423
    message: str
    resolution: str | None = None


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s[:-1] if s.endswith("Z") else s).replace(tzinfo=UTC)


def mark_decision(
    journal: sqlite3.Connection, decision_id: str, outcome: str, detail: str | None = None
) -> None:
    """decisions.outcome для еще не исполненного решения (executed не перетирается)."""
    journal.execute(
        "UPDATE decisions SET outcome=?, outcome_detail=COALESCE(?, outcome_detail) "
        "WHERE decision_id=? AND outcome='pending'",
        (outcome, detail, decision_id),
    )


def resolve_approval(
    journal: sqlite3.Connection,
    decision_id: str,
    action: str,  # approve | reject | snooze
    *,
    paused: bool = False,
    safe_mode: bool = False,
) -> ResolveOutcome:
    with transaction(journal):
        row = journal.execute(
            "SELECT resolution, expires_at, created_at FROM pending_approvals WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            return ResolveOutcome(False, 404, f"неизвестное решение {decision_id}")
        if row["resolution"] is not None:
            return ResolveOutcome(False, 409, f"уже решено: {row['resolution']}")
        now = _now_iso()
        if row["expires_at"] <= now:
            journal.execute(
                "UPDATE pending_approvals SET resolution='expired', resolved_at=? "
                "WHERE decision_id = ? AND resolution IS NULL",
                (now, decision_id),
            )
            mark_decision(journal, decision_id, "approval_expired", "ttl")
            return ResolveOutcome(False, 409, "TTL истек — карточка закрыта (fail-closed)")

        if action == "snooze":
            new_expiry_dt = datetime.now(UTC) + timedelta(minutes=SNOOZE_MINUTES)
            horizon = _parse_iso(row["created_at"]) + timedelta(minutes=MAX_CARD_LIFETIME_MIN)
            if new_expiry_dt > horizon:
                return ResolveOutcome(
                    False, 409,
                    f"лимит откладываний: карточка живет не дольше {MAX_CARD_LIFETIME_MIN} мин",
                )
            journal.execute(
                "UPDATE pending_approvals SET expires_at=? WHERE decision_id=? AND resolution IS NULL",
                (_iso(new_expiry_dt), decision_id),
            )
            return ResolveOutcome(True, 200, f"отложено на {SNOOZE_MINUTES} мин", "snoozed")

        if action == "approve" and (paused or safe_mode):
            return ResolveOutcome(False, 423, "система на паузе/в safe mode — approve невозможен")
        if action not in ("approve", "reject"):
            return ResolveOutcome(False, 404, f"неизвестное действие {action}")

        resolution = "approved" if action == "approve" else "rejected"
        cur = journal.execute(
            "UPDATE pending_approvals SET resolution=?, resolved_at=? "
            "WHERE decision_id=? AND resolution IS NULL",
            (resolution, now, decision_id),
        )
        if cur.rowcount == 0:  # кто-то успел между SELECT и UPDATE
            return ResolveOutcome(False, 409, "уже решено (параллельное нажатие)")
        if resolution == "rejected":
            mark_decision(journal, decision_id, "approval_rejected", "operator")
        msg = "взято в работу" if resolution == "approved" else "пропущено"
        return ResolveOutcome(True, 200, msg, resolution)


# ------------------------------------------------------------ approve + execute


@dataclass(frozen=True)
class ApprovalExecution:
    """Исход approve с исполнением. code 200 — исполнитель вызывался (ok/note/kind — его исход);
    иначе — отказ резолвера (404/409/423) или неверный тип входа (422), исполнения не было."""

    ok: bool
    code: int
    note: str
    kind: str = ""  # ExecutionOutcome.kind: ok | refused_precheck | exchange_rejected | ...
    entry_type: str = ""
    entry_sent: bool = False  # вход ушел на биржу: повторный approve запрещен
    resolution: str | None = None  # approved — карточка закрыта; None — откат, карточка жива

    @property
    def attempted(self) -> bool:
        return self.code == 200

    def as_dict(self, decision_id: str) -> dict:
        return {
            "decision_id": decision_id, "ok": self.ok, "note": self.note, "kind": self.kind,
            "entry_type": self.entry_type, "entry_sent": self.entry_sent,
            "resolution": self.resolution,
        }


def _trader_entry_kind(journal: sqlite3.Connection, decision_id: str) -> str | None:
    """kind опции входа, выбранной Trader'ом (decisions.trade_json + меню кандидата)."""
    try:
        row = journal.execute(
            "SELECT candidate_json, trade_json FROM decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        if row is None or not row["trade_json"]:
            return None
        option_id = json.loads(row["trade_json"]).get("entry_option_id")
        options = json.loads(row["candidate_json"]).get("entry_options") or []
        return next((str(o.get("kind")) for o in options if o.get("id") == option_id), None)
    except Exception:  # noqa: BLE001 - битая запись: фоллбек по плану
        return None


def default_entry_type(journal: sqlite3.Connection, decision_id: str) -> str:
    """Тип входа по умолчанию — выбранный Trader'ом. В плане лимитный уровень есть и у маркет-
    выбора (build_plan кладет лучший уровень меню для кнопки «Лимит»), поэтому сам выбор берем
    из decisions.trade_json; без него — по плану: entry_limit нет -> market, есть -> limit."""
    from oskar.contracts.schemas import ExecutionPlan

    try:
        row = journal.execute(
            "SELECT card_json FROM pending_approvals WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        plan = ExecutionPlan.model_validate_json(row["card_json"]) if row is not None else None
    except Exception:  # noqa: BLE001 - план не читается: исполнитель откажет сам
        plan = None
    if plan is None or plan.entry_limit is None:
        return "market"
    kind = _trader_entry_kind(journal, decision_id)
    if kind is not None:
        return "market" if kind == "market" else "limit"
    return "limit"


def approve_and_execute(
    journal: sqlite3.Connection,
    decision_id: str,
    execute_fn,  # (decision_id, entry_type) -> ExecutionOutcome: тот же, что у кнопок и автомата
    *,
    entry_type: str | None = None,  # None — выбор Trader'а (default_entry_type)
    paused: bool = False,
    safe_mode: bool = False,
) -> ApprovalExecution:
    """approve -> исполнение -> честный исход. Правила те же, что у кнопок:
    - до биржи не дошло (предпроверки, отказ биржи до входа) — approve откатывается, карточка жива;
    - вход ушел (в т.ч. «защита не встала», таймаут входа) — approve остается, повтор запрещен;
    - исключение исполнителя — исход неизвестен (мог упасть после входа): approve остается."""
    entry = entry_type or default_entry_type(journal, decision_id)
    if entry not in ENTRY_TYPES:
        return ApprovalExecution(False, 422, f"тип входа {entry!r}: только market | limit")
    out = resolve_approval(journal, decision_id, "approve", paused=paused, safe_mode=safe_mode)
    if not out.ok:
        return ApprovalExecution(False, out.code, out.message)
    try:
        res = execute_fn(decision_id, entry)
        if res is None:
            raise RuntimeError("исполнитель не вернул исход")
    except Exception as e:  # noqa: BLE001 - исполнение не роняет API
        logger.error("approve %s: execution crashed: %s", decision_id, e)
        return ApprovalExecution(
            False, 200, f"ошибка исполнения ({type(e).__name__}: {e}); исход неизвестен — проверь "
            "биржу, повторно не подтверждай", kind="error", entry_type=entry, resolution="approved",
        )
    if res.ok or res.entry_sent:
        return ApprovalExecution(res.ok, 200, res.message, res.kind, res.entry_type or entry,
                                 entry_sent=True, resolution="approved")
    with transaction(journal):  # до биржи не дошло: откат, решение снова за Иваном
        journal.execute(
            "UPDATE pending_approvals SET resolution=NULL, resolved_at=NULL "
            "WHERE decision_id=? AND resolution='approved'", (decision_id,),
        )
    return ApprovalExecution(False, 200, res.message, res.kind, entry, entry_sent=False, resolution=None)
