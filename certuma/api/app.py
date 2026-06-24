"""Dashboard backend + the Assisted-loop console (Phase 0 skeleton, Phase 1 P1.11 UI).

The internal sales lead's one screen. It renders the pending approval queue with the fully-drafted
copy each physician will receive, and Approve fires the real send: POST /approvals/{id}/decision
with decision=approved runs orchestrator.execute_approved_send (COPYWRITER draft -> Gate -> SENDER).
The kill switch and per-campaign pause remain wired to the Gate (toggling them here changes what
certuma.gate.evaluate returns before any future send). POST /events/email is the inbound webhook
seam (provider/poller signals -> certuma.monitor.ingest_event).

Styling matches the Certumalink platform (certuma/api/static/certuma.css, derived from the product
design tokens) so the console reads as the same product as the doctor profiles.

No `from __future__ import annotations` here so pydantic v2 sees real type objects on Python 3.9.
"""
import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from certuma import gate, monitor, orchestrator
from certuma.config import get_settings
from certuma.db.models import Approval, Campaign, KillSwitch, Lead, Prospect, Suppression, Template
from certuma.db.session import make_session_factory
from certuma.email import get_provider
from certuma.templates import TemplateNotFound, approve_template, lint_template

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_FONTS = ("https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800"
          "&family=Plus+Jakarta+Sans:wght@500;600;700;800&display=swap")

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


class ApproveTemplateBody(BaseModel):
    approved_by: str


class EmailEventBody(BaseModel):
    event_type: str
    dedup_key: str
    occurred_at: Optional[str] = None
    lead_id: Optional[int] = None
    message_id: Optional[int] = None
    npi: Optional[str] = None
    email: Optional[str] = None
    payload: Optional[dict] = None


_TIER_CLASS = {"high": "tier-live", "medium": "tier-review", "low": "tier-new"}


def _initials(name: str) -> str:
    parts = [p for p in (name or "").replace(".", " ").split() if p and p[0].isalpha()]
    return ((parts[0][0] + parts[-1][0]) if len(parts) >= 2 else (parts[0][:2] if parts else "Dr")).upper()


def _render_index(db: Session) -> str:
    kill = bool(db.execute(select(KillSwitch.is_active).where(KillSwitch.id == 1)).scalar())
    pending = db.execute(select(func.count()).select_from(Approval).where(Approval.state == "pending")).scalar()
    leads = db.execute(select(func.count()).select_from(Lead)).scalar()
    activated = db.execute(
        select(func.count()).select_from(Lead).where(Lead.activation_status == "physician_activated")
    ).scalar()
    suppressions = db.execute(select(func.count()).select_from(Suppression)).scalar()

    rows = db.execute(
        select(Approval, Prospect)
        .join(Lead, Approval.lead_id == Lead.id)
        .join(Prospect, Lead.npi == Prospect.npi)
        .where(Approval.state == "pending")
        .order_by(Approval.created_at)
    ).all()

    cards = []
    for a, p in rows:
        name = p.display_name or " ".join(x for x in (p.first_name, p.last_name) if x) or p.npi
        meta = " - ".join(x for x in (p.primary_specialty, p.practice_city, p.practice_state) if x)
        tier = (a.value_tier or "new").lower()
        tier_cls = _TIER_CLASS.get(tier, "tier-new")
        subj = html.escape(a.proposed_subject or "(no subject)")
        body = html.escape(a.proposed_body or "")
        cards.append(f"""
        <div class="card" data-appr="{a.id}">
          <div class="card-head">
            <span class="av">{html.escape(_initials(name))}</span>
            <div class="who">
              <div class="name">{html.escape(name)}</div>
              <div class="sub">{html.escape(meta or 'NPI ' + p.npi)}</div>
            </div>
            <span class="chip-tier {tier_cls}">{html.escape(tier)} value</span>
            <span class="chip-tier tier-ai">AI draft</span>
          </div>
          <div class="proposed">
            <div class="subj">{subj}</div>
            <div class="body">{body}</div>
          </div>
          <div class="actions">
            <button class="btn btn-primary" onclick="decide({a.id},'approved')">Approve &amp; send</button>
            <button class="btn btn-danger" onclick="decide({a.id},'rejected')">Reject</button>
          </div>
        </div>""")

    queue = "".join(cards) if cards else (
        '<div class="empty">No proposals waiting. The queue is clear.</div>')

    nav = [("Approvals", True, pending), ("Campaigns", False, None), ("Templates", False, None),
           ("Activity", False, None), ("Settings", False, None)]
    nav_html = "".join(
        f'<a class="nav-item{" active" if active else ""}" href="#">'
        f'<span class="dot"></span><span>{label}</span>'
        f'{f"<span class=nav-count>{count}</span>" if count else ""}</a>'
        for label, active, count in nav
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Certuma Reach</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="{_FONTS}">
<link rel="stylesheet" href="/static/certuma.css">
</head><body>
<div class="layout">
  <aside class="sidebar">
    <div class="brand">
      <span class="brand-logo">CR</span>
      <div><div class="brand-name">Certuma Reach</div><div class="brand-sub">Sales console</div></div>
    </div>
    <nav class="nav">{nav_html}</nav>
  </aside>
  <main class="content">
    <div id="kill-banner" class="banner{' live' if kill else ''}">
      Kill switch is ACTIVE. No emails will send until it is cleared.
    </div>
    <div class="page-head">
      <div class="t-eyebrow">Assisted outreach</div>
      <h1>Approvals</h1>
      <p>Review each AI-drafted message. Approving sends it through the compliance gate to the physician.</p>
    </div>
    <div class="kpis">
      <div class="kpi"><div class="v">{pending}</div><div class="k">Pending approvals</div></div>
      <div class="kpi"><div class="v">{leads}</div><div class="k">Leads in pipeline</div></div>
      <div class="kpi"><div class="v">{activated}</div><div class="k">Physicians activated</div></div>
      <div class="kpi"><div class="v">{suppressions}</div><div class="k">Suppressed</div></div>
    </div>
    <div class="section-title"><h2>Proposal queue</h2><span class="t-meta">{pending} waiting</span></div>
    {queue}
    <div class="foot">Certuma Reach - internal Assisted outreach. Every send is human-approved.</div>
  </main>
</div>
<script>
async function decide(id, decision) {{
  const card = document.querySelector('[data-appr="' + id + '"]');
  if (card) card.querySelectorAll('button').forEach(b => b.disabled = true);
  try {{
    const r = await fetch('/approvals/' + id + '/decision', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{decision: decision}})
    }});
    if (r.ok) {{ location.reload(); return; }}
  }} catch (e) {{}}
  alert('Action failed');
  if (card) card.querySelectorAll('button').forEach(b => b.disabled = false);
}}
</script>
</body></html>"""


def create_app(settings=None, email_provider=None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Certuma Reach dashboard", version="0.2")
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(db: Session = Depends(get_db)):
        return _render_index(db)

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

        send = None
        if body.decision == "approved":
            provider = email_provider or get_provider(settings)
            try:
                outcome = orchestrator.execute_approved_send(
                    db, appr, provider_email=provider, settings=settings)
                send = {
                    "sent": outcome.sent,
                    "reason_code": outcome.decision.reason_code if outcome.decision else None,
                    "esp_message_id": outcome.esp_message_id,
                }
            except orchestrator.OrchestratorError as exc:
                send = {"sent": False, "error": type(exc).__name__}
        db.commit()
        return {"id": approval_id, "state": body.decision, "send": send}

    @app.post("/events/email")
    def email_event(body: EmailEventBody, db: Session = Depends(get_db)):
        occurred = (datetime.fromisoformat(body.occurred_at) if body.occurred_at
                    else datetime.now(timezone.utc))
        result = monitor.ingest_event(
            db, event_type=body.event_type, dedup_key=body.dedup_key, occurred_at=occurred,
            lead_id=body.lead_id, message_id=body.message_id, npi=body.npi, email=body.email,
            payload=body.payload,
        )
        db.commit()
        return {
            "duplicate": result.duplicate,
            "transitioned_to": result.transitioned_to,
            "suppressed": result.suppressed,
            "activated": result.activated,
        }

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

    @app.get("/templates")
    def list_templates(db: Session = Depends(get_db)):
        rows = db.execute(select(Template).order_by(Template.id)).scalars().all()
        return [
            {"id": t.id, "campaign": t.campaign, "version": t.version, "subject": t.subject,
             "is_approved": t.is_approved, "approved_by": t.approved_by, "variant_label": t.variant_label}
            for t in rows
        ]

    @app.get("/templates/{template_id}/lint")
    def lint_template_endpoint(template_id: int, db: Session = Depends(get_db)):
        tpl = db.get(Template, template_id)
        if tpl is None:
            raise HTTPException(status_code=404, detail="template not found")
        problems = lint_template(tpl)
        return {"id": template_id, "ok": not problems, "problems": problems}

    @app.post("/templates/{template_id}/approve")
    def approve_template_endpoint(template_id: int, body: ApproveTemplateBody, db: Session = Depends(get_db)):
        try:
            tpl = approve_template(db, template_id, body.approved_by)
        except TemplateNotFound:
            raise HTTPException(status_code=404, detail="template not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        db.commit()
        return {"id": tpl.id, "is_approved": tpl.is_approved, "approved_by": tpl.approved_by}

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
