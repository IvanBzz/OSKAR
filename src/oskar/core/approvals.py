"""Резолвер подтверждений — единая логика для API (Hermes) и кнопок (poller).

Правила (ст. 6, TTL fail-closed): истекшее не воскрешается и помечается
expired; повторное решение отвергается; approve блокируется при pause/safe_mode
(reject разрешен всегда); snooze продлевает TTL живой карточки.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

SNOOZE_MINUTES = 15


@dataclass(frozen=True)
class ResolveOutcome:
    ok: bool
    code: int  # семантика HTTP: 200/404/409/423
    message: str
    resolution: str | None = None


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def resolve_approval(
    journal: sqlite3.Connection,
    decision_id: str,
    action: str,  # approve | reject | snooze
    *,
    paused: bool = False,
    safe_mode: bool = False,
) -> ResolveOutcome:
    row = journal.execute(
        "SELECT resolution, expires_at FROM pending_approvals WHERE decision_id = ?",
        (decision_id,),
    ).fetchone()
    if row is None:
        return ResolveOutcome(False, 404, f"неизвестное решение {decision_id}")
    if row["resolution"] is not None:
        return ResolveOutcome(False, 409, f"уже решено: {row['resolution']}")
    if row["expires_at"] <= _now_iso():
        journal.execute(
            "UPDATE pending_approvals SET resolution='expired', resolved_at=? "
            "WHERE decision_id = ?",
            (_now_iso(), decision_id),
        )
        journal.commit()
        return ResolveOutcome(False, 409, "TTL истек — карточка закрыта (fail-closed)")

    if action == "snooze":
        new_expiry = (datetime.now(UTC) + timedelta(minutes=SNOOZE_MINUTES)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        journal.execute(
            "UPDATE pending_approvals SET expires_at=? WHERE decision_id=?",
            (new_expiry, decision_id),
        )
        journal.commit()
        return ResolveOutcome(True, 200, f"отложено на {SNOOZE_MINUTES} мин", "snoozed")

    if action == "approve" and (paused or safe_mode):
        return ResolveOutcome(False, 423, "система на паузе/в safe mode — approve невозможен")
    if action not in ("approve", "reject"):
        return ResolveOutcome(False, 404, f"неизвестное действие {action}")

    resolution = "approved" if action == "approve" else "rejected"
    journal.execute(
        "UPDATE pending_approvals SET resolution=?, resolved_at=? WHERE decision_id=?",
        (resolution, _now_iso(), decision_id),
    )
    journal.commit()
    msg = "взято в работу" if resolution == "approved" else "пропущено"
    return ResolveOutcome(True, 200, msg, resolution)
