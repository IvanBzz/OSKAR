"""Резолвер подтверждений — единая логика для API (Hermes) и кнопок (poller).

Правила (ст. 6, TTL fail-closed): истекшее не воскрешается и помечается
expired; повторное решение отвергается; approve блокируется при pause/safe_mode
(reject разрешен всегда); snooze продлевает TTL живой карточки, но не дольше
MAX_CARD_LIFETIME_MIN от создания (план стареет — цена уходит).

Атомарность: решение фиксируется условным UPDATE ... WHERE resolution IS NULL
под замком журнала — гонка «кнопка + API одновременно» дает ровно одно approve.
Исход решения зеркалится в decisions.outcome (approval_rejected / approval_expired),
чтобы когорты counterfactuals не теряли отклоненные руками сетапы.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from oskar.journal.db import transaction

SNOOZE_MINUTES = 15
MAX_CARD_LIFETIME_MIN = 60  # сколько карточка может жить с учетом всех snooze


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
