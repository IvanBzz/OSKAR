"""Управляющий API демона — 127.0.0.1:8321 (бинд порта = single-instance lock).

Панель для Hermes-скиллов (oskar-control, oskar-approvals) и CLI. Никакой
торговой логики здесь: approve/reject меняют pending_approvals; исполнение
подхватывает цикл (Фаза 2). Истекший TTL необратим — fail-closed.
"""

from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException

from oskar import __version__
from oskar.core.state import RuntimeState


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def build_app(state: RuntimeState, shadow=None) -> FastAPI:
    app = FastAPI(title="oskard", version=__version__)

    @app.get("/status")
    def status():
        open_pending = state.journal.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE resolution IS NULL"
        ).fetchone()[0]
        decisions = state.journal.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        payload = {
            "run_id": state.run_id,
            "version": __version__,
            "paused": state.paused,
            "safe_mode": state.safe_mode,
            "uptime_sec": state.uptime_sec,
            "pending_approvals": open_pending,
            "decisions_total": decisions,
        }
        if shadow is not None:
            payload["shadow"] = {
                "mode": "shadow",
                "watchlist": len(shadow.watchlist),
                "ticks": shadow.ticks,
                "equity_usd": round(shadow.equity_usd, 2),
                "last_report": shadow.last_report,
            }
        return payload

    @app.get("/pending")
    def pending():
        rows = state.journal.execute(
            "SELECT decision_id, card_json, expires_at, created_at "
            "FROM pending_approvals WHERE resolution IS NULL ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def _resolve(decision_id: str, resolution: str) -> dict:
        row = state.journal.execute(
            "SELECT resolution, expires_at FROM pending_approvals WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"unknown decision {decision_id}")
        if row["resolution"] is not None:
            raise HTTPException(409, f"already resolved: {row['resolution']}")
        if row["expires_at"] <= _now_iso():
            state.journal.execute(
                "UPDATE pending_approvals SET resolution='expired', resolved_at=? "
                "WHERE decision_id = ?",
                (_now_iso(), decision_id),
            )
            state.journal.commit()
            raise HTTPException(409, "approval TTL expired (fail-closed)")
        state.journal.execute(
            "UPDATE pending_approvals SET resolution=?, resolved_at=? WHERE decision_id=?",
            (resolution, _now_iso(), decision_id),
        )
        state.journal.commit()
        return {"decision_id": decision_id, "resolution": resolution}

    @app.post("/decisions/{decision_id}/approve")
    def approve(decision_id: str):
        if state.paused or state.safe_mode:
            raise HTTPException(423, "system paused or in safe mode")
        return _resolve(decision_id, "approved")

    @app.post("/decisions/{decision_id}/reject")
    def reject(decision_id: str):
        return _resolve(decision_id, "rejected")

    @app.post("/pause")
    def pause():
        state.paused = True
        return {"paused": True}

    @app.post("/resume")
    def resume():
        if state.panic_at is not None:
            raise HTTPException(423, "panic engaged: manual operator reset required")
        state.paused = False
        return {"paused": False}

    @app.post("/panic")
    def panic():
        state.panic()  # ст. 6: ничто не смеет задержать
        return {"panic": True, "safe_mode": True, "paused": True}

    return app
