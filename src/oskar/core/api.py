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
                "equity_source": shadow.equity_source,
                "equity_at": shadow.equity_at,
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

    def _resolve(decision_id: str, action: str) -> dict:
        from oskar.core.approvals import resolve_approval

        out = resolve_approval(
            state.journal, decision_id, action,
            paused=state.paused, safe_mode=state.safe_mode,
        )
        if not out.ok:
            raise HTTPException(out.code, out.message)
        return {"decision_id": decision_id, "resolution": out.resolution}

    @app.post("/decisions/{decision_id}/approve")
    def approve(decision_id: str):
        return _resolve(decision_id, "approve")

    @app.post("/decisions/{decision_id}/reject")
    def reject(decision_id: str):
        return _resolve(decision_id, "reject")

    @app.post("/decisions/{decision_id}/snooze")
    def snooze(decision_id: str):
        return _resolve(decision_id, "snooze")

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
