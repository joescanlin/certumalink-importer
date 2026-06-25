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
import hmac
import html
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from certuma import (agents, auth, composer, docimport, engagement, gate, inbound, intelligence,
                     learning, monitor, orchestrator, reporting, support, webterm)
from certuma.classifier import StubReplyClassifier
from certuma.config import get_settings
from certuma.observability import get_logger
from certuma.reporting import queries as _rq
from certuma.db.models import (AccessLog, Approval, Campaign, ClinicianSignal, ConsoleUser, Event,
                               KillSwitch, Lead, Message, Prospect, Suppression, SupportTicket,
                               Template, Thread)

_LOG = get_logger("certuma.api")
from certuma.db.session import make_session_factory
from certuma.email import get_provider
from certuma.templates import TemplateNotFound, approve_template, lint_template

# machine endpoints a provider may post to with the webhook secret (instead of a user session)
_WEBHOOK_PATHS = ("/events/email", "/inbound/reply", "/inbound/esp", "/support/ticket")

# a 1x1 transparent GIF returned by the open-tracking pixel endpoint
_PIXEL_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00"
              b"\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")

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


def _actor(request: Request, db: Session) -> str:
    """The authenticated console username for audit attribution, or 'console' for a machine/unknown."""
    session = getattr(request, "state", None) and getattr(request.state, "user", None) or {}
    user = db.get(ConsoleUser, session["user_id"]) if session.get("user_id") else None
    return user.username if user else "console"


class KillBody(BaseModel):
    active: bool
    set_by: Optional[int] = None


class PauseBody(BaseModel):
    paused: bool


class CampaignConfigBody(BaseModel):
    is_active: Optional[bool] = None
    is_paused: Optional[bool] = None
    autonomy_level: Optional[str] = None


class DecisionBody(BaseModel):
    decision: str  # approved | rejected | edited
    decided_by: Optional[int] = None


class ApproveTemplateBody(BaseModel):
    approved_by: str


class ComposeBody(BaseModel):
    message_type: str
    model: str = composer.DEFAULT_MODEL
    brief: str = Field("", max_length=2000)
    campaign: Optional[str] = None
    specialty: str = Field("", max_length=200)


class InsertTemplateBody(BaseModel):
    subject: str = Field(..., max_length=300)
    body: str = Field(..., max_length=8000)
    message_type: str
    model: str = Field("", max_length=80)
    campaign: Optional[str] = None
    variant_label: str = Field("", max_length=8)
    approve: bool = False


class EmailEventBody(BaseModel):
    event_type: str
    dedup_key: str
    occurred_at: Optional[str] = None
    lead_id: Optional[int] = None
    message_id: Optional[int] = None
    npi: Optional[str] = None
    email: Optional[str] = None
    payload: Optional[dict] = None


class ReplyBody(BaseModel):
    reply_token: str
    text: str
    esp_message_id: str
    from_email: Optional[str] = None
    occurred_at: Optional[str] = None


class SupportTicketBody(BaseModel):
    body: str
    npi: Optional[str] = None
    subject: str = ""
    channel: str = "portal"


class AgentCreateBody(BaseModel):
    role: str
    name: str
    model: str = ""
    system_prompt: str
    activate: bool = False


class TerminalBody(BaseModel):
    command: str = Field(..., max_length=200)


class DocImportBody(BaseModel):
    rel: str = Field(..., max_length=300)
    campaign: Optional[str] = None


class SupportActionBody(BaseModel):
    action: str
    intent: Optional[str] = None


class SupportBulkBody(BaseModel):
    ids: List[int] = Field(..., max_length=500)
    action: str


class AgentUpdateBody(BaseModel):
    name: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None


_TIER_CLASS = {"high": "tier-live", "medium": "tier-review", "low": "tier-new"}
_AUTONOMY_LEVELS = ("assisted", "supervised", "autonomous")
# nav: (label, path). Only implemented screens are listed so there are no dead links.
_NAV = (("Approvals", "/"), ("Recommended", "/recommended"), ("Escalations", "/escalations"),
        ("Support", "/support"), ("Campaigns", "/campaigns"), ("Templates", "/studio"),
        ("Agents", "/agents"), ("Terminal", "/terminal"), ("Analytics", "/analytics"),
        ("Leadership", "/leadership"), ("Activity", "/activity"))

# The agent workflow, for the Agent Studio diagram: (lane, [(name, kind, model, role)]).
# kind 'llm' = a tunable Claude agent (teal); 'node' = a deterministic step (no prompt).
_PIPELINE = (
    ("Outbound", (
        ("Enricher", "node", "", "Find and verify a deliverable email; only valid contacts pass."),
        ("Copywriter", "llm", "Sonnet / Opus", "Draft the email from an approved template."),
        ("Compliance Gate", "node", "", "Suppression, CAN-SPAM, quiet hours, warmup caps, breakers."),
        ("Sender", "node", "", "At-most-once send with the List-Unsubscribe header."),
    )),
    ("Inbound", (
        ("Monitor", "node", "", "Delivered / bounce / complaint / opt-out drive the lifecycle."),
        ("Reply Classifier", "llm", "Haiku", "Label inbound reply intent."),
        ("Reply Drafter", "llm", "Opus", "Draft objection and question responses (human-approved)."),
        ("Claim Poller", "node", "", "Detect the claim-click and convert to physician_activated."),
    )),
    ("Control", (
        ("Orchestrator + Policy", "node", "", "Propose sends, apply autonomy, escalate exceptions."),
        ("Ledger-writer", "node", "", "The single writer of lead status; every move guarded and audited."),
    )),
    ("Support", (
        ("Support Classifier", "llm", "Haiku", "Label onboarding/support messages from physicians."),
        ("Support Responder", "node", "", "Answer routine tickets; escalate complaints and bugs."),
        ("Signal Feedback", "node", "", "Turn support into sales signals (upsell / referral / churn)."),
    )),
)


def _initials(name: str) -> str:
    parts = [p for p in (name or "").replace(".", " ").split() if p and p[0].isalpha()]
    return ((parts[0][0] + parts[-1][0]) if len(parts) >= 2 else (parts[0][:2] if parts else "Dr")).upper()


def _pending_count(db: Session) -> int:
    return db.execute(select(func.count()).select_from(Approval).where(Approval.state == "pending")).scalar()


def _shell(db: Session, active_path: str, *, eyebrow: str, title: str, subtitle: str, body: str) -> str:
    """Render a console page: sidebar + nav + topbar (with the global kill-switch toggle) + body."""
    kill = bool(db.execute(select(KillSwitch.is_active).where(KillSwitch.id == 1)).scalar())
    pending = _pending_count(db)
    nav_html = "".join(
        f'<a class="nav-item{" active" if path == active_path else ""}" href="{path}">'
        f'<span class="dot"></span><span>{label}</span>'
        f'{f"<span class=nav-count>{pending}</span>" if (label == "Approvals" and pending) else ""}</a>'
        for label, path in _NAV
    )
    kill_btn = (
        f'<button class="btn {"btn-danger" if not kill else "btn-secondary"}" '
        f'onclick="toggleKill({str(not kill).lower()})">'
        f'{"Pause all sending" if not kill else "Resume sending"}</button>'
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
    <form method="post" action="/logout" style="margin-top:auto;padding:14px 12px">
      <button class="nav-item" type="submit" style="width:100%;border:0;cursor:pointer">
        <span class="dot"></span><span>Sign out</span></button>
    </form>
  </aside>
  <main class="content">
    <div id="kill-banner" class="banner{' live' if kill else ''}">
      Kill switch is ACTIVE. No emails will send until it is cleared.
    </div>
    <div class="head-row">
      <div class="page-head">
        <div class="t-eyebrow">{html.escape(eyebrow)}</div>
        <h1>{html.escape(title)}</h1>
        <p>{html.escape(subtitle)}</p>
      </div>
      <div class="controls">{kill_btn}</div>
    </div>
    {body}
    <div class="foot">Certuma Reach - internal Assisted outreach. Every send is human-approved.</div>
  </main>
</div>
<script>
async function _post(url, payload) {{
  try {{
    const r = await fetch(url, {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload)}});
    if (r.ok) {{ location.reload(); return true; }}
  }} catch (e) {{}}
  alert('Action failed');
  return false;
}}
async function decide(id, decision) {{
  const card = document.querySelector('[data-appr="' + id + '"]');
  if (card) card.querySelectorAll('button').forEach(b => b.disabled = true);
  if (!await _post('/approvals/' + id + '/decision', {{decision: decision}})) {{
    if (card) card.querySelectorAll('button').forEach(b => b.disabled = false);
  }}
}}
function toggleKill(active) {{ _post('/kill-switch', {{active: active}}); }}
function campaignSet(name, field, value) {{
  const payload = {{}}; payload[field] = value;
  _post('/campaigns/' + encodeURIComponent(name), payload);
}}
async function lintTemplate(id) {{
  const el = document.getElementById('lint-' + id);
  el.className = 'lint-result'; el.textContent = 'Linting...';
  try {{
    const d = await (await fetch('/templates/' + id + '/lint')).json();
    el.className = 'lint-result ' + (d.ok ? 'ok' : 'bad');
    el.textContent = d.ok ? 'Lint passed - compliant' : ('Issues: ' + d.problems.join('; '));
  }} catch (e) {{ el.textContent = 'Lint failed to run'; }}
}}
function approveTemplate(id) {{ _post('/templates/' + id + '/approve', {{approved_by: 'console'}}); }}
function _val(scope, cls) {{ const el = scope.querySelector('.' + cls); return el ? el.value : ''; }}
function saveAgent(id) {{
  const c = document.querySelector('[data-agent="' + id + '"]');
  _post('/agents/' + id, {{name: _val(c, 'a-name'), model: _val(c, 'a-model'),
    system_prompt: _val(c, 'a-prompt')}});
}}
function activateAgent(id) {{ _post('/agents/' + id + '/activate', {{}}); }}
function rebuildAnalytics() {{ _post('/analytics/rebuild', {{}}); }}
function createAgent() {{
  const f = document.getElementById('new-agent');
  _post('/agents', {{role: _val(f, 'n-role'), name: _val(f, 'n-name'), model: _val(f, 'n-model'),
    system_prompt: _val(f, 'n-prompt'), activate: f.querySelector('.n-activate').checked}});
}}
function composeTemplate() {{
  const f = document.getElementById('compose');
  const status = document.getElementById('compose-status');
  status.className = 'lint-result'; status.textContent = 'Generating...';
  const payload = {{message_type: _val(f, 'c-type'), model: _val(f, 'c-model'),
    campaign: _val(f, 'c-campaign'), specialty: _val(f, 'c-specialty'), brief: _val(f, 'c-brief')}};
  fetch('/studio/compose', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)}}).then(r => r.ok ? r.json() : Promise.reject())
    .then(d => {{
      f.querySelector('.c-subject').value = d.subject;
      f.querySelector('.c-body').value = d.body;
      document.getElementById('compose-preview').style.display = 'block';
      status.className = 'lint-result ' + (d.ok ? 'ok' : 'bad');
      status.textContent = d.ok ? ('Drafted with ' + (d.model_label || d.model))
        : ('Drafted - fix before approving: ' + d.problems.join('; '));
    }}).catch(() => {{ status.className = 'lint-result bad'; status.textContent = 'Generate failed'; }});
}}
function insertComposed() {{
  const f = document.getElementById('compose');
  const status = document.getElementById('insert-status');
  status.className = 'lint-result'; status.textContent = 'Inserting...';
  const payload = {{subject: _val(f, 'c-subject'), body: _val(f, 'c-body'),
    message_type: _val(f, 'c-type'), model: _val(f, 'c-model'), campaign: _val(f, 'c-campaign'),
    approve: f.querySelector('.c-approve').checked}};
  fetch('/studio/templates', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)}}).then(r => r.ok ? r.json() : r.json().then(e => Promise.reject(e)))
    .then(() => location.reload())
    .catch(e => {{ status.className = 'lint-result bad';
      status.textContent = (e && e.detail) ? e.detail : 'Insert failed'; }});
}}
function _termEsc(s) {{ return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }}
function termFill(cmd) {{ const i = document.getElementById('term-cmd'); i.value = cmd; i.focus(); }}
function _termAppend(htm) {{
  const out = document.getElementById('term-out');
  out.insertAdjacentHTML('beforeend', htm);
  out.scrollTop = out.scrollHeight;
}}
async function runTerminal() {{
  const inp = document.getElementById('term-cmd');
  const cmd = inp.value.trim();
  if (!cmd) return;
  _termAppend('<div class="term-line"><span class="term-prompt">certuma&gt;</span> ' + _termEsc(cmd) + '</div>');
  inp.value = ''; inp.disabled = true;
  _termAppend('<div class="term-line t-dim" id="term-running">running...</div>');
  try {{
    const r = await fetch('/terminal/run', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{command: cmd}})}});
    const d = await r.json();
    const run = document.getElementById('term-running'); if (run) run.remove();
    if (d.error) _termAppend('<div class="term-line term-err">' + _termEsc(d.error) + '</div>');
    if (d.output) _termAppend('<pre class="term-pre">' + _termEsc(d.output) + '</pre>');
    const code = (d.exit_code === null || d.exit_code === undefined) ? '' : (' (exit ' + d.exit_code + ')');
    _termAppend('<div class="term-line ' + (d.ok ? 't-ok' : 'term-err') + '">' + (d.ok ? 'done' : 'failed') + code + '</div>');
    if (d.ok) loadDocuments();  // a run may have produced new documents
  }} catch (e) {{
    const run = document.getElementById('term-running'); if (run) run.remove();
    _termAppend('<div class="term-line term-err">request failed</div>');
  }}
  inp.disabled = false; inp.focus();
}}
function _attr(s) {{ return _termEsc(s).replace(/"/g, '&quot;'); }}
function loadDocuments() {{
  const el = document.getElementById('docs');
  if (!el) return;
  fetch('/terminal/documents').then(r => r.json()).then(d => {{
    if (!d.runs || !d.runs.length) {{
      el.innerHTML = '<div class="t-meta">No documents yet. Run <code>certumalink_run --zip 78701</code>'
        + ' (add <code>--fixture</code> for the offline sample).</div>';
      return;
    }}
    let h = '';
    d.runs.forEach(run => {{
      h += '<div class="card pad0" style="margin-bottom:12px"><div class="doc-run">' + _termEsc(run.run) + '</div>';
      h += '<table class="tbl"><thead><tr><th>Document</th><th>Rows</th><th>Size</th><th></th></tr></thead><tbody>';
      run.files.forEach(f => {{
        const imp = f.importable
          ? '<button class="btn btn-sm btn-secondary" data-import="' + _attr(f.rel) + '">Import</button>' : '';
        h += '<tr><td><a class="rowlink" href="#" data-view="' + _attr(f.rel) + '">'
          + _termEsc(f.name) + '</a></td><td class="t-meta">' + (f.rows === null ? '-' : f.rows)
          + '</td><td class="t-meta">' + f.size + ' B</td><td>' + imp + '</td></tr>';
      }});
      h += '</tbody></table></div>';
    }});
    el.innerHTML = h;
  }}).catch(() => {{}});
}}
function viewDocument(rel) {{
  fetch('/terminal/documents/view?rel=' + encodeURIComponent(rel)).then(r => r.json()).then(d => {{
    const el = document.getElementById('doc-preview');
    let h = '<div class="card"><div class="section-title"><h2>' + _termEsc(d.name) + '</h2></div>';
    if (d.kind === 'csv') {{
      h += '<div style="overflow:auto"><table class="tbl"><thead><tr>';
      d.header.forEach(c => h += '<th>' + _termEsc(c) + '</th>');
      h += '</tr></thead><tbody>';
      d.rows.forEach(row => {{ h += '<tr>'; row.forEach(c => h += '<td class="t-meta">' + _termEsc(c) + '</td>'); h += '</tr>'; }});
      h += '</tbody></table></div>';
      if (d.truncated) h += '<div class="t-meta" style="margin-top:8px">showing the first ' + d.rows.length + ' of ' + d.total_rows + ' rows</div>';
    }} else {{
      h += '<pre class="term-pre" style="background:#1c1a18;padding:12px;border-radius:10px;margin-top:8px">' + _termEsc(d.text) + '</pre>';
    }}
    h += '</div>';
    el.innerHTML = h;
    el.scrollIntoView({{behavior: 'smooth', block: 'nearest'}});
  }}).catch(() => {{ alert('Could not open that document'); }});
}}
function importDocument(rel) {{
  const v = document.getElementById('doc-campaign').value;
  const payload = {{rel: rel}};
  if (v !== '__none__') payload.campaign = v;
  fetch('/terminal/documents/import', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)}}).then(r => r.ok ? r.json() : r.json().then(e => Promise.reject(e)))
    .then(d => {{ alert('Imported ' + d.created + ' new, ' + d.updated + ' updated'
      + (d.leads_created ? (', ' + d.leads_created + ' leads') : '')
      + (d.skipped ? (', ' + d.skipped + ' skipped') : '') + '.'); }})
    .catch(e => {{ alert((e && e.detail) ? e.detail : 'Import failed'); }});
}}
function ticketAction(id, action) {{ _post('/support/' + id + '/action', {{action: action}}); }}
function ticketOverride(id) {{
  const sel = document.getElementById('ovr-' + id);
  _post('/support/' + id + '/action', {{action: 'override', intent: sel.value}});
}}
function supSel() {{
  const n = document.querySelectorAll('.t-sel:checked').length;
  const el = document.getElementById('sel-count'); if (el) el.textContent = n;
}}
function supSelAll() {{
  const all = document.getElementById('t-all').checked;
  document.querySelectorAll('.t-sel').forEach(c => {{ c.checked = all; }});
  supSel();
}}
async function bulkSupport(action) {{
  const ids = Array.from(document.querySelectorAll('.t-sel:checked')).map(c => parseInt(c.value, 10));
  if (!ids.length) {{ alert('Select at least one ticket'); return; }}
  await _post('/support/bulk', {{ids: ids, action: action}});
}}
(function () {{
  const docsEl = document.getElementById('docs');
  if (!docsEl) return;  // only on the terminal page
  docsEl.addEventListener('click', function (e) {{
    const v = e.target.closest('[data-view]');
    if (v) {{ e.preventDefault(); viewDocument(v.getAttribute('data-view')); return; }}
    const im = e.target.closest('[data-import]');
    if (im) {{ importDocument(im.getAttribute('data-import')); }}
  }});
  loadDocuments();  // surface documents from earlier runs on load
}})();
</script>
</body></html>"""


def _approvals_body(db: Session) -> str:
    pending = _pending_count(db)
    leads = db.execute(select(func.count()).select_from(Lead)).scalar()
    activated = db.execute(
        select(func.count()).select_from(Lead).where(Lead.activation_status == "physician_activated")
    ).scalar()
    suppressions = db.execute(select(func.count()).select_from(Suppression)).scalar()

    rows = db.execute(
        select(Approval, Prospect)
        .join(Lead, Approval.lead_id == Lead.id)
        .join(Prospect, Lead.npi == Prospect.npi)
        .where(Approval.state == "pending", Approval.proposed_action != "reply")  # replies live on Escalations
        .order_by(Approval.created_at)
    ).all()

    cards = []
    for a, p in rows:
        name = p.display_name or " ".join(x for x in (p.first_name, p.last_name) if x) or p.npi
        meta = " - ".join(x for x in (p.primary_specialty, p.practice_city, p.practice_state) if x)
        tier = (a.value_tier or "new").lower()
        tier_cls = _TIER_CLASS.get(tier, "tier-new")
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
            <div class="subj">{html.escape(a.proposed_subject or '(no subject)')}</div>
            <div class="body">{html.escape(a.proposed_body or '')}</div>
          </div>
          <div class="actions">
            <button class="btn btn-primary" onclick="decide({a.id},'approved')">Approve &amp; send</button>
            <button class="btn btn-danger" onclick="decide({a.id},'rejected')">Reject</button>
          </div>
        </div>""")
    queue = "".join(cards) if cards else '<div class="empty">No proposals waiting. The queue is clear.</div>'
    return f"""
    <div class="kpis">
      <div class="kpi"><div class="v">{pending}</div><div class="k">Pending approvals</div></div>
      <div class="kpi"><div class="v">{leads}</div><div class="k">Leads in pipeline</div></div>
      <div class="kpi"><div class="v">{activated}</div><div class="k">Physicians activated</div></div>
      <div class="kpi"><div class="v">{suppressions}</div><div class="k">Suppressed</div></div>
    </div>
    <div class="section-title"><h2>Proposal queue</h2><span class="t-meta">{pending} waiting</span></div>
    {queue}"""


def _campaigns_body(db: Session) -> str:
    camps = db.execute(select(Campaign).order_by(Campaign.name)).scalars().all()
    counts = dict(db.execute(select(Lead.campaign, func.count()).group_by(Lead.campaign)).all())
    rows = []
    for c in camps:
        if not c.name:  # skip the '' sentinel campaign
            continue
        opts = "".join(
            f'<option value="{lvl}"{" selected" if c.autonomy_level == lvl else ""}>{lvl}</option>'
            for lvl in _AUTONOMY_LEVELS
        )
        active_pill = ('<span class="pill pill-on">active</span>' if c.is_active
                       else '<span class="pill pill-off">inactive</span>')
        paused_pill = ('<span class="pill pill-warn">paused</span>' if c.is_paused
                       else '<span class="pill pill-on">running</span>')
        nm = html.escape(c.name)
        rows.append(f"""
        <tr>
          <td><a class="cell-title rowlink" href="/campaigns/{nm}">{html.escape(c.label or c.name)}</a>
              <div class="t-meta">{nm}</div></td>
          <td>{active_pill}</td>
          <td>{paused_pill}</td>
          <td class="tabular">{counts.get(c.name, 0)}</td>
          <td><select class="sel" onchange="campaignSet('{nm}','autonomy_level',this.value)">{opts}</select></td>
          <td class="row-actions">
            <button class="btn btn-sm btn-secondary" onclick="campaignSet('{nm}','is_active',{str(not c.is_active).lower()})">
              {"Deactivate" if c.is_active else "Activate"}</button>
            <button class="btn btn-sm btn-secondary" onclick="campaignSet('{nm}','is_paused',{str(not c.is_paused).lower()})">
              {"Resume" if c.is_paused else "Pause"}</button>
          </td>
        </tr>""")
    body = "".join(rows) if rows else '<tr><td colspan="6" class="empty-cell">No campaigns.</td></tr>'
    return f"""
    <div class="card pad0">
      <table class="tbl">
        <thead><tr><th>Campaign</th><th>Active</th><th>Sending</th><th>Leads</th>
          <th>Autonomy</th><th></th></tr></thead>
        <tbody>{body}</tbody>
      </table>
    </div>
    <div class="t-meta" style="margin-top:12px">Autonomy: assisted = every send human-approved
      (supervised / autonomous are Phase 2).</div>"""


def _campaign_detail_body(db: Session, name: str) -> str:
    c = db.get(Campaign, name)
    if c is None:
        return ('<div class="empty">That campaign does not exist. '
                '<a href="/campaigns">Back to campaigns</a>.</div>')
    nm = html.escape(name)
    active_pill = ('<span class="pill pill-on">active</span>' if c.is_active
                   else '<span class="pill pill-off">inactive</span>')
    paused_pill = ('<span class="pill pill-warn">paused</span>' if c.is_paused
                   else '<span class="pill pill-on">running</span>')
    opts = "".join(
        f'<option value="{lvl}"{" selected" if c.autonomy_level == lvl else ""}>{lvl}</option>'
        for lvl in _AUTONOMY_LEVELS)

    status_counts = db.execute(
        select(Lead.activation_status, func.count()).where(Lead.campaign == name)
        .group_by(Lead.activation_status).order_by(func.count().desc())).all()
    total = sum(n for _, n in status_counts)
    funnel_rows = "".join(
        f'<tr><td><div class="cell-title">{html.escape(st)}</div></td>'
        f'<td class="tabular">{n}</td></tr>' for st, n in status_counts
    ) or '<tr><td colspan="2" class="empty-cell">No leads in this campaign yet.</td></tr>'

    templates = db.execute(
        select(Template).where(Template.campaign == name).order_by(Template.version)).scalars().all()
    _trs = []
    for t in templates:
        sub = f" - {html.escape(t.variant_label)}" if t.variant_label else ""
        mt = (f" - {html.escape(composer.MESSAGE_TYPE_LABELS.get(t.message_type, t.message_type))}"
              if t.message_type else "")
        state = ('<span class="pill pill-on">approved</span>' if t.is_approved
                 else '<span class="pill pill-warn">draft</span>')
        who = (_MODEL_LABELS.get(t.model or "", t.model or "") if (t.source or "") == "ai" else "manual")
        _trs.append(
            f'<tr><td><div class="cell-title">{html.escape(t.subject)}</div>'
            f'<div class="t-meta">v{t.version}{sub}{mt}</div></td>'
            f'<td>{state}</td><td class="t-meta">{html.escape(who)}</td></tr>')
    tmpl_rows = "".join(_trs) or '<tr><td colspan="3" class="empty-cell">No templates for this campaign.</td></tr>'

    msgs = db.execute(
        select(Message, Prospect).join(Prospect, Message.npi == Prospect.npi, isouter=True)
        .where(Message.campaign == name).order_by(Message.id.desc()).limit(10)).all()
    msg_rows = "".join(
        f'<tr><td><div class="cell-title">{html.escape((p.display_name if p else None) or (m.npi or ""))}</div></td>'
        f'<td class="t-meta">{html.escape(m.direction or "")} {html.escape(m.channel or "")}</td>'
        f'<td class="t-meta">{html.escape((m.subject or m.body_rendered or "")[:60])}</td></tr>'
        for m, p in msgs
    ) or '<tr><td colspan="3" class="empty-cell">No messages yet.</td></tr>'

    return f"""
    <div class="head-row" style="margin-bottom:14px">
      <a class="t-meta" href="/campaigns">&larr; Back to campaigns</a></div>
    <div class="card">
      <div class="card-head">
        <div class="who"><div class="name">{html.escape(c.label or c.name)}</div>
          <div class="sub">{nm} - {total} leads</div></div>
        {active_pill}{paused_pill}
      </div>
      <div class="field"><label>Autonomy</label>
        <select class="sel" onchange="campaignSet('{nm}','autonomy_level',this.value)">{opts}</select></div>
      <div class="actions">
        <button class="btn btn-sm btn-secondary" onclick="campaignSet('{nm}','is_active',{str(not c.is_active).lower()})">
          {"Deactivate" if c.is_active else "Activate"}</button>
        <button class="btn btn-sm btn-secondary" onclick="campaignSet('{nm}','is_paused',{str(not c.is_paused).lower()})">
          {"Resume sending" if c.is_paused else "Pause sending"}</button>
      </div>
    </div>
    <div class="section-title" style="margin-top:22px"><h2>Lead funnel</h2></div>
    <div class="card pad0"><table class="tbl">
      <thead><tr><th>Status</th><th>Leads</th></tr></thead><tbody>{funnel_rows}</tbody></table></div>
    <div class="section-title" style="margin-top:22px"><h2>Templates</h2></div>
    <div class="card pad0"><table class="tbl">
      <thead><tr><th>Template</th><th>State</th><th>Authored by</th></tr></thead>
      <tbody>{tmpl_rows}</tbody></table></div>
    <div class="section-title" style="margin-top:22px"><h2>Recent activity</h2></div>
    <div class="card pad0"><table class="tbl">
      <thead><tr><th>Clinician</th><th>Touch</th><th>Subject</th></tr></thead>
      <tbody>{msg_rows}</tbody></table></div>"""


_MODEL_LABELS = {mid: label for label, mid in composer.MODELS}


def _compose_panel(db: Session) -> str:
    """The AI compose card: pick a model + message type + campaign, write a brief, generate copy."""
    model_opts = "".join(
        f'<option value="{html.escape(mid)}">{html.escape(label)}</option>'
        for label, mid in composer.MODELS)
    type_opts = "".join(
        f'<option value="{html.escape(k)}">{html.escape(v)}</option>' for k, v in composer.MESSAGE_TYPES)
    camps = db.execute(select(Campaign.name).order_by(Campaign.name)).scalars().all()
    camp_opts = '<option value="">general (all campaigns)</option>' + "".join(
        f'<option value="{html.escape(c)}">{html.escape(c)}</option>' for c in camps)
    return f"""
    <div class="card" id="compose">
      <div class="section-title"><h2>Compose with AI</h2>
        <span class="t-meta">author an outbound or follow-up message, then insert it as an A/B variant</span></div>
      <div class="grid-2">
        <div class="field"><label>Model</label><select class="sel c-model">{model_opts}</select></div>
        <div class="field"><label>Message type</label><select class="sel c-type">{type_opts}</select></div>
      </div>
      <div class="grid-2">
        <div class="field"><label>Campaign</label><select class="sel c-campaign">{camp_opts}</select></div>
        <div class="field"><label>Specialty focus (optional)</label>
          <input class="inp c-specialty" placeholder="e.g. Dermatology"></div>
      </div>
      <div class="field"><label>Brief</label>
        <textarea class="ta c-brief" rows="3" placeholder="Angle / tone / what to emphasize..."></textarea></div>
      <div class="actions">
        <button class="btn btn-sm btn-primary" onclick="composeTemplate()">Generate</button>
        <span id="compose-status" class="lint-result"></span>
      </div>
      <div id="compose-preview" style="display:none;margin-top:14px">
        <div class="field"><label>Subject</label><input class="inp c-subject"></div>
        <div class="field"><label>Body</label><textarea class="ta c-body" rows="9"></textarea></div>
        <label class="check"><input type="checkbox" class="c-approve"> Approve on insert (must pass lint)</label>
        <div class="actions">
          <button class="btn btn-sm btn-primary" onclick="insertComposed()">Insert as variant</button>
          <span id="insert-status" class="lint-result"></span>
        </div>
      </div>
    </div>"""


def _studio_body(db: Session) -> str:
    templates = db.execute(select(Template).order_by(Template.campaign, Template.version)).scalars().all()
    cards = []
    for t in templates:
        scope = html.escape(t.campaign) if t.campaign else "all campaigns"
        variant = f" - {html.escape(t.variant_label)}" if t.variant_label else ""
        status = ('<span class="pill pill-on">approved</span>' if t.is_approved
                  else '<span class="pill pill-warn">draft</span>')
        approver = f' by {html.escape(t.approved_by)}' if (t.is_approved and t.approved_by) else ""
        mtype = composer.MESSAGE_TYPE_LABELS.get(t.message_type or "", t.message_type or "")
        tags = f'<span class="chip-tier tier-new">{html.escape(mtype)}</span>' if mtype else ""
        if (t.source or "") == "ai":
            model_lbl = _MODEL_LABELS.get(t.model or "", t.model or "AI")
            tags += f'<span class="chip-tier tier-ai">AI - {html.escape(model_lbl)}</span>'
        approve_btn = (
            f'<button class="btn btn-sm btn-primary" onclick="approveTemplate({t.id})">Approve</button>'
            if not t.is_approved else "")
        cards.append(f"""
        <div class="card">
          <div class="card-head">
            <div class="who">
              <div class="name">{html.escape(t.subject)}</div>
              <div class="sub">{scope}{variant} - v{t.version}{approver}</div>
            </div>
            {status}
          </div>
          <div class="chips">{tags}</div>
          <div class="proposed"><div class="body">{html.escape(t.body)}</div></div>
          <div class="actions">
            <button class="btn btn-sm btn-secondary" onclick="lintTemplate({t.id})">Run linter</button>
            {approve_btn}
            <span id="lint-{t.id}" class="lint-result"></span>
          </div>
        </div>""")
    body = "".join(cards) if cards else '<div class="empty">No templates yet.</div>'

    # one label can be authored by different models across message types/campaigns (a variant spans
    # the cadence); show the model only when it is unambiguous, else "mixed" - never a wrong single.
    label_models: dict = {}
    for t in templates:
        who = (_MODEL_LABELS.get(t.model or "", t.model or "AI") if (t.source or "") == "ai" else "manual")
        label_models.setdefault(t.variant_label or "", set()).add(who)

    def _authored(label) -> str:
        whos = label_models.get(str(label))
        return "-" if not whos else (next(iter(whos)) if len(whos) == 1 else "mixed")

    perf = learning.variant_performance(db)
    winner = learning.winning_variant(db)
    perf_rows = "".join(
        f'<tr><td><div class="cell-title">{html.escape(str(r["variant"]))}'
        f'{" &#11088; winner" if r["variant"] == winner else ""}</div></td>'
        f'<td class="t-meta">{html.escape(_authored(r["variant"]))}</td>'
        f'<td class="tabular">{r["sent"]}</td><td class="tabular">{r["replied"]}</td>'
        f'<td class="tabular">{r["activated"]}</td>'
        f'<td class="tabular">{("-" if r["activation_rate"] is None else str(r["activation_rate"]) + "%")}</td></tr>'
        for r in perf
    ) or '<tr><td colspan="6" class="empty-cell">No variant data yet (one approved template, no A/B).</td></tr>'

    return f"""
    {_compose_panel(db)}
    <div class="section-title" style="margin-top:22px"><h2>Templates</h2>
      <span class="t-meta">{len(templates)} total</span></div>
    {body}
    <div class="section-title" style="margin-top:22px"><h2>Variant performance</h2>
      <span class="t-meta">A/B by activation rate{(" - winner: " + html.escape(winner)) if winner else ""}</span></div>
    <div class="card pad0"><table class="tbl">
      <thead><tr><th>Variant</th><th>Authored by</th><th>Sent</th><th>Replied</th><th>Activated</th><th>Activation rate</th></tr></thead>
      <tbody>{perf_rows}</tbody></table></div>"""


def _activity_body(db: Session) -> str:
    sent = db.execute(
        select(func.count()).select_from(Message).where(Message.direction == "outbound")
    ).scalar()
    delivered = db.execute(
        select(func.count()).select_from(Message).where(
            Message.direction == "outbound", Message.delivered.is_(True))
    ).scalar()
    activated = db.execute(
        select(func.count()).select_from(Lead).where(Lead.activation_status == "physician_activated")
    ).scalar()

    status_counts = dict(
        db.execute(select(Lead.activation_status, func.count()).group_by(Lead.activation_status)).all()
    )
    status_rows = "".join(
        f'<tr><td>{html.escape(s)}</td><td class="tabular">{n}</td></tr>'
        for s, n in sorted(status_counts.items(), key=lambda kv: -kv[1])
    ) or '<tr><td colspan="2" class="empty-cell">No leads yet.</td></tr>'

    supps = db.execute(
        select(Suppression).order_by(Suppression.created_at.desc()).limit(10)
    ).scalars().all()
    supp_rows = "".join(
        f'<tr><td><span class="pill pill-warn">{html.escape(s.reason)}</span></td>'
        f'<td class="t-meta">{html.escape(s.email or s.npi or "")}</td></tr>'
        for s in supps
    ) or '<tr><td colspan="2" class="empty-cell">No suppressions.</td></tr>'

    events = db.execute(select(Event).order_by(Event.occurred_at.desc()).limit(12)).scalars().all()
    event_rows = "".join(
        f'<tr><td>{html.escape(e.event_type)}</td>'
        f'<td class="t-meta">{html.escape(e.npi or "")}</td></tr>'
        for e in events
    ) or '<tr><td colspan="2" class="empty-cell">No events yet.</td></tr>'

    def pct(n):
        return f"{round(100 * n / sent)}%" if sent else "-"

    _STATE_PILL = {"churn_risk": "pill-warn", "went_quiet": "pill-warn", "opened_no_reply": "pill-on"}
    eng_rows = "".join(
        f'<tr><td><div class="cell-title">{html.escape(r["name"])}</div>'
        f'<div class="t-meta">{html.escape(r["specialty"] or "NPI " + r["npi"])}</div></td>'
        f'<td><span class="pill {_STATE_PILL.get(r["state"], "pill-off")}">'
        f'{html.escape(r["state"].replace("_", " "))}</span></td>'
        f'<td class="tabular">{r["open_count"]}</td>'
        f'<td><div class="cell-title">{html.escape(r["play"] or "")}</div></td></tr>'
        for r in engagement.engagement_queue(db, limit=25)
    ) or '<tr><td colspan="4" class="empty-cell">No engagement plays right now.</td></tr>'

    return f"""
    <div class="section-title"><h2>Conversion funnel</h2></div>
    <div class="kpis">
      <div class="kpi"><div class="v">{sent}</div><div class="k">Emails sent</div></div>
      <div class="kpi"><div class="v">{delivered}</div><div class="k">Delivered ({pct(delivered)})</div></div>
      <div class="kpi"><div class="v">{activated}</div><div class="k">Activated ({pct(activated)})</div></div>
    </div>
    <div class="grid-2">
      <div class="card pad0">
        <table class="tbl"><thead><tr><th>Lead status</th><th>Count</th></tr></thead>
          <tbody>{status_rows}</tbody></table>
      </div>
      <div class="card pad0">
        <table class="tbl"><thead><tr><th>Recent suppressions</th><th></th></tr></thead>
          <tbody>{supp_rows}</tbody></table>
      </div>
    </div>
    <div class="section-title" style="margin-top:22px"><h2>Re-engage &amp; churn risk</h2>
      <span class="t-meta">engagement signal plays</span></div>
    <div class="card pad0">
      <table class="tbl"><thead><tr><th>Clinician</th><th>Signal</th><th>Opens</th><th>Play</th></tr></thead>
        <tbody>{eng_rows}</tbody></table>
    </div>
    <div class="section-title" style="margin-top:22px"><h2>Recent events</h2></div>
    <div class="card pad0">
      <table class="tbl"><thead><tr><th>Event</th><th>NPI</th></tr></thead>
        <tbody>{event_rows}</tbody></table>
    </div>"""


def _workflow_diagram() -> str:
    lanes = []
    for lane, nodes in _PIPELINE:
        boxes = []
        for i, (name, kind, model, role) in enumerate(nodes):
            if i:
                boxes.append('<div class="arrow">&rarr;</div>')
            chip = (f'<span class="chip-tier tier-ai">{html.escape(model)}</span>' if kind == "llm"
                    else '<span class="node-kind">deterministic</span>')
            boxes.append(
                f'<div class="node{" llm" if kind == "llm" else ""}">'
                f'<div class="node-name">{html.escape(name)}</div>{chip}'
                f'<div class="node-role">{html.escape(role)}</div></div>')
        lanes.append(f'<div class="lane"><div class="lane-label">{html.escape(lane)}</div>'
                     f'<div class="flow">{"".join(boxes)}</div></div>')
    return f'<div class="card diagram">{"".join(lanes)}</div>'


def _agents_body(db: Session) -> str:
    cards = []
    for a in agents.list_agents(db):
        role_label = agents.ROLE_LABELS.get(a.role, a.role)
        right = ('<span class="pill pill-on">active</span>' if a.is_active
                 else f'<button class="btn btn-sm btn-secondary" onclick="activateAgent({a.id})">Make active</button>')
        cards.append(f"""
        <div class="card" data-agent="{a.id}">
          <div class="card-head">
            <div class="who"><div class="name">{html.escape(a.name)}</div>
              <div class="sub">{html.escape(role_label)} - v{a.version}</div></div>
            {right}
          </div>
          <div class="field"><label>Name</label>
            <input class="inp a-name" value="{html.escape(a.name)}"></div>
          <div class="field"><label>Model</label>
            <input class="inp a-model" value="{html.escape(a.model)}"></div>
          <div class="field"><label>System prompt</label>
            <textarea class="ta a-prompt" rows="6">{html.escape(a.system_prompt)}</textarea></div>
          <div class="actions">
            <button class="btn btn-sm btn-primary" onclick="saveAgent({a.id})">Save changes</button></div>
        </div>""")

    role_opts = "".join(
        f'<option value="{r}">{html.escape(agents.ROLE_LABELS[r])}</option>' for r in agents.ROLES)
    new_form = f"""
    <div class="card" id="new-agent">
      <div class="section-title"><h2>Spin up a fresh agent</h2>
        <span class="t-meta">a new prompt/model variant for a role</span></div>
      <div class="grid-2">
        <div class="field"><label>Role</label><select class="sel n-role">{role_opts}</select></div>
        <div class="field"><label>Name</label>
          <input class="inp n-name" placeholder="e.g. Warm dermatology copywriter"></div>
      </div>
      <div class="field"><label>Model</label>
        <input class="inp n-model" placeholder="claude-sonnet-4-6"></div>
      <div class="field"><label>System prompt</label>
        <textarea class="ta n-prompt" rows="6" placeholder="Instructions for this agent..."></textarea></div>
      <label class="check"><input type="checkbox" class="n-activate"> Make this the active agent for its role</label>
      <div class="actions"><button class="btn btn-sm btn-primary" onclick="createAgent()">Create agent</button></div>
    </div>"""

    return f"""
    <div class="section-title"><h2>Workflow</h2><span class="t-meta">how the agents hand off</span></div>
    {_workflow_diagram()}
    <div class="section-title" style="margin-top:26px"><h2>Agents</h2>
      <span class="t-meta">edit a prompt, then Save - it drives the live Claude agent</span></div>
    {"".join(cards)}
    {new_form}"""


_TERM_EXAMPLES = {"certumalink_run": "certumalink_run --zip 78701"}


def _terminal_body(db: Session) -> str:
    palette = "".join(
        f'<button class="btn btn-sm btn-secondary" '
        f'onclick="termFill(\'{html.escape(_TERM_EXAMPLES.get(c.key, c.key))}\')">'
        f'{html.escape(c.label)}</button>' for c in webterm.COMMANDS.values())
    cmd_rows = "".join(
        f'<tr><td><code>{html.escape(c.key)}</code></td>'
        f'<td class="t-meta">{html.escape(c.description)}</td></tr>'
        for c in webterm.COMMANDS.values())
    camps = db.execute(select(Campaign.name).order_by(Campaign.name)).scalars().all()
    camp_opts = ('<option value="__none__">prospects only</option>' + "".join(
        f'<option value="{html.escape(c)}"{" selected" if c == "primary-care" else ""}>'
        f'{html.escape(c)}</option>' for c in camps if c))
    return f"""
    <div class="section-title"><h2>Console</h2>
      <span class="t-meta">run the real CLI data tools; this is an allowlist, not a shell</span></div>
    <div class="term" id="term">
      <div class="term-out" id="term-out"><div class="term-line t-dim">Type a command, or `help`. Try `certumalink_run --zip 78701` or `seed-active`.</div></div>
      <div class="term-input">
        <span class="term-prompt">certuma&gt;</span>
        <input class="term-cmd" id="term-cmd" autocomplete="off" spellcheck="false"
               placeholder="certumalink_run --zip 78701" onkeydown="if(event.key==='Enter')runTerminal()">
        <button class="btn btn-sm btn-primary" onclick="runTerminal()">Run</button>
      </div>
    </div>
    <div class="term-palette">{palette}</div>
    <div class="section-title" style="margin-top:22px"><h2>Generated documents</h2>
      <span class="t-meta">what certumalink_run produced - preview and import them</span>
      <span class="docimport-pick">import into
        <select class="sel" id="doc-campaign">{camp_opts}</select></span></div>
    <div id="docs"><div class="t-meta">No documents yet. Run
      <code>certumalink_run --zip 78701</code> (add <code>--fixture</code> to use the bundled sample offline).</div></div>
    <div id="doc-preview"></div>
    <div class="section-title" style="margin-top:22px"><h2>Commands</h2></div>
    <div class="card pad0"><table class="tbl">
      <thead><tr><th>Command</th><th>What it does</th></tr></thead>
      <tbody>{cmd_rows}</tbody></table></div>"""


def _escalations_body(db: Session) -> str:
    # 1. drafted objection/question replies waiting for a human
    reply_rows = db.execute(
        select(Approval, Prospect)
        .join(Lead, Approval.lead_id == Lead.id)
        .join(Prospect, Lead.npi == Prospect.npi)
        .where(Approval.state == "pending", Approval.proposed_action == "reply")
        .order_by(Approval.created_at)
    ).all()
    reply_cards = []
    for a, p in reply_rows:
        name = p.display_name or " ".join(x for x in (p.first_name, p.last_name) if x) or p.npi
        inbound_msg = db.execute(
            select(Message).where(Message.lead_id == a.lead_id, Message.direction == "inbound")
            .order_by(Message.id.desc()).limit(1)).scalar()
        their_reply = html.escape(inbound_msg.body_rendered) if (inbound_msg and inbound_msg.body_rendered) else ""
        reply_cards.append(f"""
        <div class="card" data-appr="{a.id}">
          <div class="card-head">
            <span class="av">{html.escape(_initials(name))}</span>
            <div class="who"><div class="name">{html.escape(name)}</div>
              <div class="sub">replied with a {html.escape(a.gate_reason_code or 'question')}</div></div>
            <span class="chip-tier tier-review">needs a human</span>
          </div>
          <div class="proposed"><div class="subj">Their reply</div>
            <div class="body">{their_reply}</div></div>
          <div class="proposed"><div class="subj">Suggested response <span class="chip-tier tier-ai">AI draft</span></div>
            <div class="body">{html.escape(a.proposed_body or '')}</div></div>
          <div class="actions">
            <button class="btn btn-primary" onclick="decide({a.id},'approved')">Approve reply</button>
            <button class="btn btn-danger" onclick="decide({a.id},'rejected')">Reject</button>
          </div>
        </div>""")

    # 2. needs_review leads that do not yet have a drafted reply (wrong-person, lint failures, etc.)
    drafted_lead_ids = {a.lead_id for a, _ in reply_rows}
    nr_rows = db.execute(
        select(Lead, Prospect).join(Prospect, Lead.npi == Prospect.npi)
        .where(Lead.activation_status == "needs_review").order_by(Lead.id)
    ).all()
    nr_items = []
    for lead, p in nr_rows:
        if lead.id in drafted_lead_ids:
            continue
        name = p.display_name or " ".join(x for x in (p.first_name, p.last_name) if x) or p.npi
        nr_items.append(
            f'<tr><td><div class="cell-title">{html.escape(name)}</div>'
            f'<div class="t-meta">NPI {html.escape(p.npi)}</div></td>'
            f'<td><span class="pill pill-warn">needs review</span></td></tr>')
    nr_table = ("".join(nr_items) if nr_items
                else '<tr><td colspan="2" class="empty-cell">Nothing else needs review.</td></tr>')

    reply_section = ("".join(reply_cards) if reply_cards
                     else '<div class="empty">No replies waiting for a response.</div>')
    return f"""
    <div class="section-title"><h2>Replies to handle</h2>
      <span class="t-meta">{len(reply_cards)} drafted, awaiting your approval</span></div>
    {reply_section}
    <div class="section-title" style="margin-top:24px"><h2>Other items needing review</h2></div>
    <div class="card pad0"><table class="tbl">
      <thead><tr><th>Lead</th><th>Status</th></tr></thead><tbody>{nr_table}</tbody></table></div>"""


_SUPPORT_STATUS_PILL = {"open": "pill-warn", "escalated": "pill-warn", "answered": "pill-on",
                        "resolved": "pill-on"}
_SUPPORT_SIGNAL_LABEL = {"expansion_intent": "Upsell lead", "advocate": "Referral lead",
                         "churn_risk_support": "Churn risk"}
_SUPPORT_SIGNAL_PILL = {"expansion_intent": "tier-live", "advocate": "tier-done",
                        "churn_risk_support": "tier-review"}


def _support_body(db: Session) -> str:
    tickets = db.execute(
        select(SupportTicket, Prospect)
        .join(Prospect, SupportTicket.npi == Prospect.npi, isouter=True)
        .order_by(SupportTicket.id.desc()).limit(40)
    ).all()
    trows = []
    for t, p in tickets:
        name = (p.display_name if p else None) or (t.npi or "unknown")
        intent = (t.intent or "unclassified").replace("_", " ")
        status_pill = _SUPPORT_STATUS_PILL.get(t.status, "pill-off")
        resolution = (html.escape(t.answer) if t.answer else
                      ("escalated to a human" if t.status == "escalated" else ""))
        trows.append(
            f'<tr><td class="selcell"><input type="checkbox" class="t-sel" value="{t.id}" '
            f'onclick="supSel()"></td>'
            f'<td><a class="cell-title rowlink" href="/support/{t.id}">{html.escape(name)}</a>'
            f'<div class="t-meta">{html.escape(t.body or "")[:90]}</div></td>'
            f'<td><span class="chip-tier tier-ai">{html.escape(intent)}</span></td>'
            f'<td><span class="pill {status_pill}">{html.escape(t.status)}</span></td>'
            f'<td class="t-meta">{resolution}</td></tr>')
    ticket_rows = "".join(trows) or '<tr><td colspan="5" class="empty-cell">No support tickets yet.</td></tr>'

    sigs = db.execute(
        select(ClinicianSignal, Prospect)
        .join(Prospect, ClinicianSignal.npi == Prospect.npi, isouter=True)
        .where(ClinicianSignal.source == "support").order_by(ClinicianSignal.id.desc()).limit(40)
    ).all()
    srows = []
    for s, p in sigs:
        name = (p.display_name if p else None) or (s.npi or "")
        label = _SUPPORT_SIGNAL_LABEL.get(s.signal_type, s.signal_type)
        pill = _SUPPORT_SIGNAL_PILL.get(s.signal_type, "tier-new")
        srows.append(
            f'<tr><td><div class="cell-title">{html.escape(name)}</div></td>'
            f'<td><span class="chip-tier {pill}">{html.escape(label)}</span></td>'
            f'<td class="t-meta">from a {html.escape((s.value or "").replace("_", " "))} message</td></tr>')
    signal_rows = "".join(srows) or ('<tr><td colspan="3" class="empty-cell">No sales signals from '
                                     'support yet.</td></tr>')

    return f"""
    <div class="section-title"><h2>Support tickets</h2>
      <span class="t-meta">click a clinician to open the ticket; select rows for a bulk action</span></div>
    <div class="bulkbar">
      <label class="check"><input type="checkbox" id="t-all" onclick="supSelAll()"> select all</label>
      <span class="t-meta"><span id="sel-count">0</span> selected</span>
      <button class="btn btn-sm btn-secondary" onclick="bulkSupport('resolved')">Resolve selected</button>
      <button class="btn btn-sm btn-secondary" onclick="bulkSupport('escalated')">Escalate selected</button>
    </div>
    <div class="card pad0"><table class="tbl">
      <thead><tr><th></th><th>Clinician</th><th>Intent</th><th>Status</th><th>Resolution</th></tr></thead>
      <tbody>{ticket_rows}</tbody></table></div>
    <div class="section-title" style="margin-top:24px"><h2>Sales signals from support</h2>
      <span class="t-meta">support interactions feeding the sales knowledge graph</span></div>
    <div class="card pad0"><table class="tbl">
      <thead><tr><th>Clinician</th><th>Signal</th><th>Source</th></tr></thead>
      <tbody>{signal_rows}</tbody></table></div>"""


def _support_detail_body(db: Session, ticket_id: int) -> str:
    row = db.execute(
        select(SupportTicket, Prospect)
        .join(Prospect, SupportTicket.npi == Prospect.npi, isouter=True)
        .where(SupportTicket.id == ticket_id)
    ).first()
    if row is None:
        return '<div class="empty">That support ticket does not exist. <a href="/support">Back to support</a>.</div>'
    t, p = row
    name = (p.display_name if p else None) or (t.npi or "unknown")
    meta = " - ".join(x for x in ((p.primary_specialty if p else ""), (t.npi or "")) if x)
    status_pill = _SUPPORT_STATUS_PILL.get(t.status, "pill-off")
    intent = (t.intent or "unclassified").replace("_", " ")
    created = t.created_at.strftime("%Y-%m-%d %H:%M UTC") if t.created_at else ""
    answer = (f'<div class="proposed"><div class="subj">Agent answer</div>'
              f'<div class="body">{html.escape(t.answer)}</div></div>' if t.answer else "")
    escalated_note = ('<div class="t-meta" style="margin-top:8px">This ticket is escalated to a human.</div>'
                      if t.status == "escalated" else "")

    sig = None
    if t.npi:
        sig = db.execute(select(ClinicianSignal).where(
            ClinicianSignal.npi == t.npi, ClinicianSignal.source == "support",
            ClinicianSignal.signal_type == (t.emitted_signal or "")).limit(1)).scalar() if t.emitted_signal else None
    if sig is not None:
        label = _SUPPORT_SIGNAL_LABEL.get(sig.signal_type, sig.signal_type)
        pill = _SUPPORT_SIGNAL_PILL.get(sig.signal_type, "tier-new")
        signal_card = (f'<div class="card"><div class="section-title"><h2>Sales signal</h2></div>'
                       f'<span class="chip-tier {pill}">{html.escape(label)}</span>'
                       f'<div class="t-meta" style="margin-top:8px">written to the knowledge graph that '
                       f'sales scoring reads (source: support)</div></div>')
    else:
        signal_card = ('<div class="card"><div class="section-title"><h2>Sales signal</h2></div>'
                       '<div class="t-meta">no sales signal from this ticket</div></div>')

    others = db.execute(
        select(SupportTicket).where(SupportTicket.npi == t.npi, SupportTicket.id != t.id)
        .order_by(SupportTicket.id.desc()).limit(8)) if t.npi else None
    other_rows = "".join(
        f'<tr><td><a class="rowlink" href="/support/{o.id}">{html.escape((o.subject or o.body or "")[:60])}</a></td>'
        f'<td><span class="pill {_SUPPORT_STATUS_PILL.get(o.status, "pill-off")}">{html.escape(o.status)}</span></td></tr>'
        for o in (others.scalars().all() if others is not None else [])
    ) or '<tr><td colspan="2" class="empty-cell">No other tickets from this clinician.</td></tr>'

    intent_opts = "".join(
        f'<option value="{i}"{" selected" if i == t.intent else ""}>{html.escape(i.replace("_", " "))}</option>'
        for i in support.SUPPORT_INTENTS)

    return f"""
    <div class="head-row" style="margin-bottom:14px">
      <a class="t-meta" href="/support">&larr; Back to support</a></div>
    <div class="card">
      <div class="card-head">
        <div class="who"><div class="name">{html.escape(name)}</div>
          <div class="sub">{html.escape(meta)} - {html.escape(t.channel or "portal")} - {html.escape(created)}</div></div>
        <span class="pill {status_pill}">{html.escape(t.status)}</span>
      </div>
      <div class="chips"><span class="chip-tier tier-ai">{html.escape(intent)}</span></div>
      <div class="proposed"><div class="subj">{html.escape(t.subject or "(no subject)")}</div>
        <div class="body">{html.escape(t.body or "")}</div></div>
      {answer}{escalated_note}
      <div class="actions" style="margin-top:12px;flex-wrap:wrap">
        <button class="btn btn-sm btn-secondary" onclick="ticketAction({t.id},'reclassify')">Re-classify</button>
        <button class="btn btn-sm btn-secondary" onclick="ticketAction({t.id},'escalate')">Escalate</button>
        <button class="btn btn-sm btn-primary" onclick="ticketAction({t.id},'resolve')">Resolve</button>
      </div>
      <div class="field" style="margin-top:12px"><label>Override intent</label>
        <div class="actions">
          <select class="sel" id="ovr-{t.id}">{intent_opts}</select>
          <button class="btn btn-sm btn-secondary" onclick="ticketOverride({t.id})">Apply intent</button>
        </div>
      </div>
    </div>
    {signal_card}
    <div class="section-title" style="margin-top:22px"><h2>Other tickets from this clinician</h2></div>
    <div class="card pad0"><table class="tbl">
      <thead><tr><th>Ticket</th><th>Status</th></tr></thead><tbody>{other_rows}</tbody></table></div>"""


def _dim_table(title: str, rows: list) -> str:
    body = "".join(
        f'<tr><td><div class="cell-title">{html.escape(str(r["label"]))}</div></td>'
        f'<td class="tabular">{r["leads"]}</td><td class="tabular">{r["sent"]}</td>'
        f'<td class="tabular">{r["activated"]}</td>'
        f'<td class="tabular">{("-" if r["activation_rate"] is None else str(r["activation_rate"]) + "%")}</td></tr>'
        for r in rows
    ) or '<tr><td colspan="5" class="empty-cell">No data yet.</td></tr>'
    return f"""
    <div class="section-title" style="margin-top:22px"><h2>{html.escape(title)}</h2></div>
    <div class="card pad0"><table class="tbl">
      <thead><tr><th>{html.escape(title.split(' ')[-1].title())}</th><th>Leads</th><th>Sent</th>
        <th>Activated</th><th>Activation rate</th></tr></thead>
      <tbody>{body}</tbody></table></div>"""


_FIT_CLASS = {"high": "tier-live", "medium": "tier-review", "low": "tier-new"}


def _recommended_body(db: Session) -> str:
    rows = intelligence.recommended_actions(db, limit=50)
    items = []
    for r in rows:
        meta = " - ".join(x for x in (r["specialty"], r["state"]) if x)
        items.append(
            f'<tr><td><div class="cell-title">{html.escape(r["name"])}</div>'
            f'<div class="t-meta">{html.escape(meta or "NPI " + r["npi"])}</div></td>'
            f'<td><span class="chip-tier {_FIT_CLASS.get(r["fit_tier"], "tier-new")}">'
            f'{r["fit_score"]} {html.escape(r["fit_tier"])}</span></td>'
            f'<td class="t-meta">{html.escape(r["status"])}</td>'
            f'<td><div class="cell-title">{html.escape(r["action"])}</div>'
            f'<div class="t-meta">{html.escape(r["reason"])}</div></td></tr>')
    body = "".join(items) if items else '<tr><td colspan="4" class="empty-cell">No open leads.</td></tr>'
    return f"""
    <div class="section-title"><h2>Recommended actions</h2>
      <span class="t-meta">open leads plus support-driven upsell &amp; retention, ranked by fit</span></div>
    <div class="card pad0"><table class="tbl">
      <thead><tr><th>Clinician</th><th>Fit</th><th>Status</th><th>Next best action</th></tr></thead>
      <tbody>{body}</tbody></table></div>"""


def _analytics_body(db: Session) -> str:
    f = _rq.funnel_totals(db)
    eco = _rq.unit_economics(db)
    ttd = _rq.time_to_activation_days(db)
    asof = _rq.rebuilt_at(db)
    asof_str = asof.strftime("%Y-%m-%d %H:%M UTC") if asof else "never (click Rebuild)"

    def rate(v):
        return "-" if v is None else f"{v}%"

    stages = [("Universe", f["universe"]), ("Enriched", f["enriched"]), ("Sent", f["sent"]),
              ("Delivered", f["delivered"]), ("Opened", f["opened"]), ("Replied", f["replied"]),
              ("Activated", f["activated"])]
    funnel = "".join(
        f'<div class="kpi"><div class="v">{val}</div><div class="k">{label}</div></div>'
        for label, val in stages)
    rates = [("Delivery", rate(f["delivery_rate"])), ("Open", rate(f["open_rate"])),
             ("Reply", rate(f["reply_rate"])), ("Activation", rate(f["activation_rate"]))]
    rate_kpis = "".join(
        f'<div class="kpi"><div class="v">{val}</div><div class="k">{label} rate</div></div>'
        for label, val in rates)
    cpa = "-" if eco["cost_per_activation"] is None else f"${eco['cost_per_activation']}"
    ttd_str = "-" if ttd is None else f"{ttd}d"
    eco_kpis = (
        f'<div class="kpi"><div class="v">${eco["total_send_cost"]}</div><div class="k">Total send cost</div></div>'
        f'<div class="kpi"><div class="v">{cpa}</div><div class="k">Cost / activation</div></div>'
        f'<div class="kpi"><div class="v">{ttd_str}</div><div class="k">Avg time to activation</div></div>')

    return f"""
    <div class="head-row" style="margin-bottom:14px">
      <div class="t-meta">Customer Intelligence as of {html.escape(asof_str)}</div>
      <button class="btn btn-sm btn-secondary" onclick="rebuildAnalytics()">Rebuild</button>
    </div>
    <div class="section-title"><h2>Conversion funnel</h2><span class="t-meta">excludes suppressed</span></div>
    <div class="kpis" style="grid-template-columns:repeat(7,1fr)">{funnel}</div>
    <div class="kpis">{rate_kpis}</div>
    <div class="section-title" style="margin-top:22px"><h2>Unit economics</h2></div>
    <div class="kpis" style="grid-template-columns:repeat(3,1fr)">{eco_kpis}</div>
    {_dim_table("Conversion by specialty", _rq.by_dimension(db, "specialty"))}
    {_dim_table("Conversion by campaign", _rq.by_dimension(db, "campaign"))}
    <div class="section-title" style="margin-top:22px"><h2>Touches by channel</h2></div>
    <div class="card pad0"><table class="tbl">
      <thead><tr><th>Channel</th><th>Touches</th><th>Delivered</th></tr></thead>
      <tbody>{"".join(f'<tr><td><div class="cell-title">{html.escape(c["channel"])}</div></td>'
                      f'<td class="tabular">{c["touches"]}</td><td class="tabular">{c["delivered"]}</td></tr>'
                      for c in _rq.touches_by_channel(db))
              or '<tr><td colspan=3 class="empty-cell">No touches yet.</td></tr>'}</tbody></table></div>"""


def _render_login(error: str = "") -> str:
    err = (f'<div class="banner live" style="display:block;margin-bottom:14px">{html.escape(error)}</div>'
           if error else "")
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in - Certuma Reach</title>
<link rel="stylesheet" href="{_FONTS}"><link rel="stylesheet" href="/static/certuma.css"></head>
<body><div style="max-width:360px;margin:13vh auto;padding:0 20px">
  <div class="brand" style="padding:0 0 18px"><span class="brand-logo">CR</span>
    <div><div class="brand-name">Certuma Reach</div><div class="brand-sub">Sales console</div></div></div>
  <div class="card">{err}
    <form method="post" action="/login">
      <div class="field"><label>Username</label><input class="inp" name="username" autofocus></div>
      <div class="field"><label>Password</label><input class="inp" type="password" name="password"></div>
      <div class="actions"><button class="btn btn-primary" type="submit" style="width:100%">Sign in</button></div>
    </form></div></div></body></html>"""


def _leadership_body(db: Session) -> str:
    f = _rq.funnel_totals(db)
    eco = _rq.unit_economics(db)
    cpa = "-" if eco["cost_per_activation"] is None else f"${eco['cost_per_activation']}"
    kpis = [("Universe", f["universe"]), ("Activated", f["activated"]),
            ("Activation rate", "-" if f["activation_rate"] is None else f"{f['activation_rate']}%"),
            ("Cost / activation", cpa)]
    kpi_html = "".join(f'<div class="kpi"><div class="v">{v}</div><div class="k">{k}</div></div>'
                       for k, v in kpis)
    return f"""
    <div class="section-title"><h2>Program outcomes</h2><span class="t-meta">read-only</span></div>
    <div class="kpis" style="grid-template-columns:repeat(4,1fr)">{kpi_html}</div>
    {_dim_table("Conversion by specialty", _rq.by_dimension(db, "specialty"))}"""


def create_app(settings=None, email_provider=None, classifier=None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Certuma Reach dashboard", version="0.2")
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # session secret: from settings, else a per-process random one (dev). Sessions reset on restart.
    secret = settings.session_secret
    if not secret:
        secret = os.urandom(32).hex()
        _LOG.warning("CERTUMA_SESSION_SECRET is not set; using an ephemeral per-process secret. "
                     "Sessions will not survive a restart and will be invalid across workers. "
                     "Set CERTUMA_SESSION_SECRET in any multi-process or production deployment.")

    def _user_of(request: Request) -> Optional[dict]:
        token = request.cookies.get(auth.SESSION_COOKIE)
        return auth.verify_session(token, secret=secret) if token else None

    @app.middleware("http")
    async def _auth_mw(request: Request, call_next):
        path, method = request.url.path, request.method
        public = (path == "/login" or path.startswith("/static/") or path.startswith("/track/open/"))
        user = _user_of(request)
        request.state.user = user
        if public:
            return await call_next(request)
        # machine webhook auth (P3.10): a provider posting events/replies presents the shared secret
        # instead of a session. Off by default (no secret configured) so nothing is silently open.
        if (path in _WEBHOOK_PATHS and settings.webhook_secret and hmac.compare_digest(
                request.headers.get("x-certuma-webhook-secret", ""), settings.webhook_secret)):
            request.state.user = {"role": "operator", "user_id": 0}  # machine has write capability
            return await call_next(request)
        if user is None:  # not signed in
            if method == "GET":
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        # RBAC: leadership is read-only - only operator/admin may mutate (logout is always allowed)
        if method != "GET" and path != "/logout" and not auth.can_write(user.get("role")):
            return JSONResponse({"detail": "this role is read-only"}, status_code=403)
        return await call_next(request)

    @app.get("/login", response_class=HTMLResponse)
    def login_form():
        return _render_login()

    @app.post("/login")
    def login(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
        user = auth.authenticate(db, username=username, password=password)
        if user is None:
            db.add(AccessLog(username=username, action="login_failed", path="/login"))
            db.commit()
            return HTMLResponse(_render_login("Invalid username or password."), status_code=401)
        token = auth.sign_session(user.id, user.role, secret=secret)
        db.add(AccessLog(username=user.username, role=user.role, action="login", path="/login"))
        db.commit()
        resp = RedirectResponse("/", status_code=303)
        # NOTE: set secure=True behind HTTPS in production; httponly + samesite mitigate XSS/CSRF.
        resp.set_cookie(auth.SESSION_COOKIE, token, httponly=True, samesite="lax",
                        max_age=auth.SESSION_TTL)
        return resp

    @app.post("/logout")
    def logout(request: Request, db: Session = Depends(get_db)):
        session = getattr(request.state, "user", None) or {}
        user = db.get(ConsoleUser, session["user_id"]) if session.get("user_id") else None
        db.add(AccessLog(username=(user.username if user else None), role=session.get("role"),
                         action="logout"))
        db.commit()
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.SESSION_COOKIE)
        return resp

    @app.get("/leadership", response_class=HTMLResponse)
    def leadership_page(db: Session = Depends(get_db)):
        return _shell(db, "/leadership", eyebrow="Leadership", title="Leadership view",
                      subtitle="High-level program outcomes (read-only).", body=_leadership_body(db))

    @app.get("/", response_class=HTMLResponse)
    def index(db: Session = Depends(get_db)):
        return _shell(db, "/", eyebrow="Assisted outreach", title="Approvals",
                      subtitle="Review each AI-drafted message. Approving sends it through the "
                               "compliance gate to the physician.",
                      body=_approvals_body(db))

    @app.get("/recommended", response_class=HTMLResponse)
    def recommended_page(db: Session = Depends(get_db)):
        return _shell(db, "/recommended", eyebrow="Intelligence", title="Recommended actions",
                      subtitle="Open leads ranked by fit (knowledge-graph signals), each with its "
                               "next-best-action.",
                      body=_recommended_body(db))

    @app.get("/support", response_class=HTMLResponse)
    def support_page(db: Session = Depends(get_db)):
        return _shell(db, "/support", eyebrow="Customer success", title="Support",
                      subtitle="The support agents classify onboarding questions, answer or escalate, "
                               "and feed sales signals back into the knowledge graph.",
                      body=_support_body(db))

    @app.post("/support/ticket")
    def support_ticket(body: SupportTicketBody, db: Session = Depends(get_db)):
        ticket, outcome = support.handle_ticket(db, npi=body.npi, body=body.body, subject=body.subject,
                                                channel=body.channel)
        db.commit()
        return {"ticket_id": ticket.id, "intent": outcome.intent, "status": outcome.status,
                "sales_signal": outcome.sales_signal}

    @app.post("/support/bulk")
    def support_bulk(body: SupportBulkBody, db: Session = Depends(get_db)):
        if body.action not in ("resolved", "escalated"):
            raise HTTPException(status_code=400, detail="unknown bulk action")
        count = support.bulk_set_status(db, body.ids, body.action)
        db.commit()
        return {"ok": True, "updated": count}

    @app.get("/support/{ticket_id}", response_class=HTMLResponse)
    def support_detail_page(ticket_id: int, db: Session = Depends(get_db)):
        return _shell(db, "/support", eyebrow="Customer success", title="Support ticket",
                      subtitle="The full ticket, the sales signal it produced, and the actions you can take.",
                      body=_support_detail_body(db, ticket_id))

    @app.post("/support/{ticket_id}/action")
    def support_ticket_action(ticket_id: int, body: SupportActionBody, db: Session = Depends(get_db)):
        try:
            if body.action == "reclassify":
                support.reclassify(db, ticket_id)
            elif body.action == "resolve":
                support.set_status(db, ticket_id, "resolved")
            elif body.action == "escalate":
                support.set_status(db, ticket_id, "escalated")
            elif body.action == "override":
                if not body.intent:
                    raise HTTPException(status_code=400, detail="intent is required to override")
                support.override_intent(db, ticket_id, body.intent)
            else:
                raise HTTPException(status_code=400, detail="unknown action")
        except KeyError:
            raise HTTPException(status_code=404, detail="ticket not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        db.commit()
        ticket = db.get(SupportTicket, ticket_id)
        return {"ok": True, "ticket_id": ticket_id, "intent": ticket.intent, "status": ticket.status}

    @app.get("/escalations", response_class=HTMLResponse)
    def escalations_page(db: Session = Depends(get_db)):
        return _shell(db, "/escalations", eyebrow="Human in the loop", title="Escalations",
                      subtitle="Replies the agents drafted for your approval, and anything else that "
                               "needs a human.",
                      body=_escalations_body(db))

    @app.get("/campaigns", response_class=HTMLResponse)
    def campaigns_page(db: Session = Depends(get_db)):
        return _shell(db, "/campaigns", eyebrow="Configuration", title="Campaigns",
                      subtitle="Activate, pause, and set the autonomy level for each outreach campaign.",
                      body=_campaigns_body(db))

    @app.get("/campaigns/{name}", response_class=HTMLResponse)
    def campaign_detail_page(name: str, db: Session = Depends(get_db)):
        return _shell(db, "/campaigns", eyebrow="Configuration", title="Campaign",
                      subtitle="Configuration, lead funnel, templates, and recent activity for this campaign.",
                      body=_campaign_detail_body(db, name))

    @app.get("/studio", response_class=HTMLResponse)
    def studio_page(db: Session = Depends(get_db)):
        return _shell(db, "/studio", eyebrow="Copy", title="Template studio",
                      subtitle="Compose copy with AI, lint and approve it, and A/B test the variants "
                               "the copywriter drafts from.",
                      body=_studio_body(db))

    @app.post("/studio/compose")
    def studio_compose(body: ComposeBody, db: Session = Depends(get_db)):
        if body.message_type not in composer.MESSAGE_TYPE_KEYS:
            raise HTTPException(status_code=400, detail="unknown message_type")
        model = body.model or composer.DEFAULT_MODEL
        if model not in composer.MODEL_IDS:
            raise HTTPException(status_code=400, detail="unknown model")
        req = composer.ComposeRequest(message_type=body.message_type, brief=body.brief,
                                      campaign=body.campaign or None, specialty=body.specialty,
                                      model=model)
        try:
            result = composer.compose_template(db, request=req, settings=settings)
        except Exception as exc:  # a refusal or provider error must not 500 the studio
            _LOG.warning("compose_failed", extra={"error": str(exc)})
            raise HTTPException(status_code=502, detail="generation failed, try again or edit by hand")
        return {"subject": result.subject, "body": result.body, "model": result.model,
                "model_label": _MODEL_LABELS.get(result.model, result.model),
                "message_type": result.message_type, "ok": result.ok, "problems": list(result.problems)}

    @app.post("/studio/templates")
    def studio_insert_template(request: Request, body: InsertTemplateBody, db: Session = Depends(get_db)):
        try:
            tpl = composer.insert_template(
                db, campaign=body.campaign or None, subject=body.subject, body=body.body,
                message_type=body.message_type, model=body.model, variant_label=body.variant_label,
                created_by=_actor(request, db), approve=body.approve)
        except ValueError as exc:
            db.rollback()  # discard the flushed-but-non-compliant draft
            raise HTTPException(status_code=400, detail=str(exc))
        except IntegrityError:  # a concurrent insert took the same (campaign, version)
            db.rollback()
            raise HTTPException(status_code=409, detail="a version conflict occurred, please retry")
        db.commit()
        return {"id": tpl.id, "campaign": tpl.campaign, "version": tpl.version,
                "variant_label": tpl.variant_label, "message_type": tpl.message_type,
                "model": tpl.model, "is_approved": tpl.is_approved}

    @app.get("/terminal", response_class=HTMLResponse)
    def terminal_page(db: Session = Depends(get_db)):
        return _shell(db, "/terminal", eyebrow="Operations", title="Terminal",
                      subtitle="Run the platform's data tools (the doctor importer, seeders, the "
                               "scheduler) from an allowlisted in-console terminal.",
                      body=_terminal_body(db))

    @app.post("/terminal/run")
    def terminal_run(request: Request, body: TerminalBody):
        # defense in depth: the middleware already blocks read-only roles on non-GET, assert it here too
        if not auth.can_write((getattr(request.state, "user", None) or {}).get("role")):
            raise HTTPException(status_code=403, detail="this role is read-only")
        result = webterm.run_command(body.command)
        return {"ok": result.ok, "command": result.command, "exit_code": result.exit_code,
                "output": result.output, "error": result.error}

    @app.get("/terminal/documents")
    def terminal_documents():
        return {"runs": webterm.list_documents()}

    @app.get("/terminal/documents/view")
    def terminal_document_view(rel: str):
        try:
            return webterm.read_document(rel)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/terminal/documents/import")
    def terminal_document_import(request: Request, body: DocImportBody, db: Session = Depends(get_db)):
        if not auth.can_write((getattr(request.state, "user", None) or {}).get("role")):
            raise HTTPException(status_code=403, detail="this role is read-only")
        try:
            result = docimport.import_document(db, body.rel, campaign=(body.campaign or None))
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc))
        except (IntegrityError, DataError):
            db.rollback()
            raise HTTPException(status_code=400, detail="the document could not be imported")
        db.commit()
        return {"created": result.created, "updated": result.updated,
                "leads_created": result.leads_created, "skipped": result.skipped,
                "total": result.total}

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics_page(db: Session = Depends(get_db)):
        return _shell(db, "/analytics", eyebrow="Evidence", title="Analytics",
                      subtitle="Customer Intelligence: which specialties, regions, and campaigns convert.",
                      body=_analytics_body(db))

    @app.post("/analytics/rebuild")
    def analytics_rebuild(db: Session = Depends(get_db)):
        report = reporting.rebuild(db)
        db.commit()
        return {"clinicians": report.clinicians, "touches": report.touches, "leads": report.leads}

    @app.get("/activity", response_class=HTMLResponse)
    def activity_page(db: Session = Depends(get_db)):
        return _shell(db, "/activity", eyebrow="Pipeline", title="Activity",
                      subtitle="The send-to-activation funnel, suppressions, and recent lifecycle events.",
                      body=_activity_body(db))

    @app.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request, db: Session = Depends(get_db)):
        # seed the defaults on first OPERATOR visit only; a read-only role never mutates on a GET
        if auth.can_write((getattr(request.state, "user", None) or {}).get("role")):
            agents.ensure_seeded(db)
            db.commit()
        return _shell(db, "/agents", eyebrow="Configuration", title="Agent Studio",
                      subtitle="See how the agents hand off, tune each agent's prompt, and spin up new ones.",
                      body=_agents_body(db))

    @app.post("/agents")
    def create_agent(body: AgentCreateBody, db: Session = Depends(get_db)):
        try:
            a = agents.create_agent(db, role=body.role, name=body.name, model=body.model,
                                    system_prompt=body.system_prompt, activate=body.activate)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        db.commit()
        return {"id": a.id, "role": a.role, "is_active": a.is_active}

    @app.post("/agents/{agent_id}")
    def update_agent(agent_id: int, body: AgentUpdateBody, db: Session = Depends(get_db)):
        try:
            a = agents.update_agent(db, agent_id, name=body.name, model=body.model,
                                    system_prompt=body.system_prompt)
        except KeyError:
            raise HTTPException(status_code=404, detail="agent not found")
        db.commit()
        return {"id": a.id, "version": a.version}

    @app.post("/agents/{agent_id}/activate")
    def activate_agent(agent_id: int, db: Session = Depends(get_db)):
        try:
            a = agents.activate_agent(db, agent_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="agent not found")
        db.commit()
        return {"id": a.id, "is_active": True}

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
        # 'reply' approvals (drafted objection responses) are human-handled, not auto-sent: the
        # threaded-reply send path is intentionally not wired (see reply_drafter). Mark state only.
        if body.decision == "approved" and appr.proposed_action != "reply":
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

    @app.get("/track/open/{token}")
    def track_open(token: str, db: Session = Depends(get_db)):
        # the open-tracking pixel: map the token back to a lead and record a (weak) opened event
        thread = db.execute(select(Thread).where(Thread.reply_token == token)).scalar()
        if thread is not None:
            now = datetime.now(timezone.utc)
            monitor.ingest_event(db, event_type="opened",
                                 dedup_key=f"open:{token}:{now.date().isoformat()}",
                                 occurred_at=now, lead_id=thread.lead_id)
            db.commit()
        return Response(content=_PIXEL_GIF, media_type="image/gif",
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    @app.post("/inbound/esp")
    def inbound_esp(payload: dict, db: Session = Depends(get_db)):
        # the real ESP/IMAP inbound webhook seam: normalize the provider payload, then handle it
        fields = inbound.parse_esp_inbound(payload)
        if fields is None:
            return {"matched": False, "reason": "no reply token in payload"}
        res, outcome = inbound.handle_reply(
            db, occurred_at=datetime.now(timezone.utc),
            classifier=classifier or StubReplyClassifier(), **fields)
        db.commit()
        return {"matched": res.matched, "duplicate": res.duplicate,
                "intent": outcome.intent if outcome else None}

    @app.post("/inbound/reply")
    def inbound_reply(body: ReplyBody, db: Session = Depends(get_db)):
        occurred = (datetime.fromisoformat(body.occurred_at) if body.occurred_at
                    else datetime.now(timezone.utc))
        res, outcome = inbound.handle_reply(
            db, reply_token=body.reply_token, text=body.text, esp_message_id=body.esp_message_id,
            from_email=body.from_email, occurred_at=occurred,
            classifier=classifier or StubReplyClassifier(),
        )
        db.commit()
        return {
            "matched": res.matched,
            "duplicate": res.duplicate,
            "lead_id": res.lead_id,
            "intent": outcome.intent if outcome else None,
            "transitioned_to": outcome.transitioned_to if outcome else res.transitioned_to,
            "escalated": outcome.escalated if outcome else False,
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

    @app.post("/campaigns/{name}")
    def update_campaign(name: str, body: CampaignConfigBody, db: Session = Depends(get_db)):
        values = {k: v for k, v in body.model_dump().items() if v is not None}
        if not values:
            raise HTTPException(status_code=400, detail="no fields to update")
        if "autonomy_level" in values and values["autonomy_level"] not in _AUTONOMY_LEVELS:
            raise HTTPException(status_code=400, detail="invalid autonomy_level")
        result = db.execute(update(Campaign).where(Campaign.name == name).values(**values))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="campaign not found")
        db.commit()
        return {"campaign": name, **values}

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
