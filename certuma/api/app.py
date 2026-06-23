"""Dashboard backend (Phase 0 skeleton).

Scope: a read-only pipeline/health view, the approval-queue read + decide stubs, and the
global kill switch + per-campaign pause WIRED TO THE GATE (the load-bearing part: toggling the
switch here changes what certuma.gate.evaluate returns before any future send). Everything else
(funnel analytics, deliverability panel, template studio, real auth) is deferred to later phases.

No `from __future__ import annotations` here so pydantic v2 sees real type objects on Python 3.9.
"""
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from certuma import gate
from certuma.db.models import Approval, Campaign, KillSwitch, Lead, Prospect, Suppression
from certuma.db.session import make_session_factory

_SessionFactory = None


def _session_factory():
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = make_session_factory()
    return _SessionFactory


def get_db():
    """Per-request session dependency. Overridden in tests to a rolled-back session."""
    db = _session_factory()()
    try:
        yield db
    finally:
        db.close()


class KillBody(BaseModel):
    active: bool
    set_by: Optional[int] = None


class PauseBody(BaseModel):
    paused: bool


class DecisionBody(BaseModel):
    decision: str  # approved | rejected | edited
    decided_by: Optional[int] = None


def create_app() -> FastAPI:
    app = FastAPI(title="Certuma Reach dashboard", version="0.1")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (
            "<!doctype html><title>Certuma Reach</title>"
            "<h1>Certuma Reach - dashboard skeleton</h1>"
            "<p>Phase 0. Endpoints: "
            "<a href='/health'>/health</a>, /approvals, /kill-switch, /campaigns/{name}/pause, "
            "/gate/preview</p>"
        )

    @app.get("/health")
    def health(db: Session = Depends(get_db)):
        status_counts = dict(
            db.execute(select(Lead.activation_status, func.count()).group_by(Lead.activation_status)).all()
        )
        return {
            "leads": db.execute(select(func.count()).select_from(Lead)).scalar(),
            "prospects": db.execute(select(func.count()).select_from(Prospect)).scalar(),
            "suppressions": db.execute(select(func.count()).select_from(Suppression)).scalar(),
            "campaigns": db.execute(select(func.count()).select_from(Campaign)).scalar(),
            "pending_approvals": db.execute(
                select(func.count()).select_from(Approval).where(Approval.state == "pending")
            ).scalar(),
            "kill_switch_active": bool(
                db.execute(select(KillSwitch.is_active).where(KillSwitch.id == 1)).scalar()
            ),
            "status_counts": status_counts,
        }

    @app.get("/approvals")
    def approvals(state: str = "pending", db: Session = Depends(get_db)):
        rows = db.execute(
            select(Approval, Prospect.display_name)
            .join(Lead, Approval.lead_id == Lead.id)
            .join(Prospect, Lead.npi == Prospect.npi)
            .where(Approval.state == state)
            .order_by(Approval.created_at)
        ).all()
        return [
            {
                "id": a.id,
                "lead_id": a.lead_id,
                "display_name": name,
                "proposed_action": a.proposed_action,
                "value_tier": a.value_tier,
                "gate_reason_code": a.gate_reason_code,
                "model_confidence": float(a.model_confidence) if a.model_confidence is not None else None,
                "state": a.state,
            }
            for a, name in rows
        ]

    @app.post("/approvals/{approval_id}/decision")
    def decide(approval_id: int, body: DecisionBody, db: Session = Depends(get_db)):
        if body.decision not in ("approved", "rejected", "edited"):
            raise HTTPException(status_code=400, detail="invalid decision")
        appr = db.get(Approval, approval_id)
        if appr is None:
            raise HTTPException(status_code=404, detail="approval not found")
        appr.state = body.decision
        appr.decided_by = body.decided_by
        appr.decided_at = func.now()
        db.commit()
        return {"id": approval_id, "state": body.decision}

    @app.post("/kill-switch")
    def kill_switch(body: KillBody, db: Session = Depends(get_db)):
        db.execute(
            update(KillSwitch).where(KillSwitch.id == 1).values(
                is_active=body.active, set_by=body.set_by, set_at=func.now()
            )
        )
        db.commit()
        return {"kill_switch_active": body.active}

    @app.post("/campaigns/{name}/pause")
    def pause_campaign(name: str, body: PauseBody, db: Session = Depends(get_db)):
        result = db.execute(update(Campaign).where(Campaign.name == name).values(is_paused=body.paused))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="campaign not found")
        db.commit()
        return {"campaign": name, "is_paused": body.paused}

    @app.get("/gate/preview")
    def gate_preview(
        npi: Optional[str] = None,
        email: Optional[str] = None,
        campaign: Optional[str] = None,
        db: Session = Depends(get_db),
    ):
        decision = gate.evaluate(db, npi=npi, email=email, campaign=campaign)
        return {"decision": decision.decision, "reason_code": decision.reason_code}

    return app


app = create_app()  # module-level app for `uvicorn certuma.api.app:app`
