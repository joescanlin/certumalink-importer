"""Active-campaign demo seed (rich data for the dashboard).

Generates a large, realistic snapshot - many clinicians across specialties/regions spread over the
whole lifecycle (enriching -> sendable -> sent -> awaiting/opened/quiet -> replied/objection ->
activated / do-not-contact / exhausted), with knowledge-graph signals, A/B variants that have enough
sample for a winner to emerge, multi-channel touches, opens, suppressions, and drafted-reply
escalations - so every console screen presents like an in-flight campaign. Deterministic (derived
from the index, no randomness). `make seed-active` runs it; it commits.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from certuma import reporting, signals, support
from certuma.config import Settings, get_settings
from certuma.db.models import (Approval, Campaign, ClinicianSignal, Contact, Event, Lead, Mailbox,
                               Message, PracticeGroup, Prospect, Suppression, SupportTicket, Template,
                               Thread, WorkflowScore)
from certuma_core.learning import assign_variant

__all__ = ["seed_active", "main", "SPECIALTIES", "VARIANTS"]

NOW = datetime(2026, 6, 24, 15, tzinfo=timezone.utc)
VARIANTS = ["A", "B", "C"]
_ACT_RATE = {"A": 26, "B": 14, "C": 7}  # variant-weighted activation % -> A is the winner

SPECIALTIES = ["Dermatology", "Cardiology", "Pediatrics", "Oncology", "Orthopedics", "Psychiatry",
               "Family Medicine", "Internal Medicine", "Neurology", "Gastroenterology",
               "Endocrinology", "Rheumatology"]
_STATES = ["TX", "CA", "NY", "FL", "IL", "PA", "OH", "GA", "NC", "MI"]
_CITIES = ["Austin", "Los Angeles", "New York", "Miami", "Chicago", "Philadelphia", "Columbus",
           "Atlanta", "Charlotte", "Detroit"]
_FIRST = ["Mara", "Liam", "Priya", "Evan", "Sara", "Noah", "Ava", "Owen", "Maya", "Jack", "Zoe",
          "Ivan", "Lena", "Cole", "Nina", "Ravi", "Tess", "Dev", "Iris", "Sam"]
_LAST = ["Singh", "Ortega", "Nguyen", "Brooks", "Kim", "Patel", "Cohen", "Reyes", "Walsh", "Diaz",
         "Frost", "Mehta", "Yoon", "Park", "Lowe", "Shah", "Wells", "Roy", "Bauer", "Cruz"]
_BODY = ("Hi Dr. {last_name}, your {pitch_angle} in {city}. Review your profile: {claim_url}. "
         "Unsubscribe: {unsubscribe_url}. {postal_address}")


def _pct(npi: str, salt: str) -> int:
    return int(hashlib.md5(f"{npi}:{salt}".encode()).hexdigest(), 16) % 100


def seed_active(session: Session, *, settings: Optional[Settings] = None, n: int = 160,
                when: datetime = NOW) -> dict:
    """Seed n clinicians spread across the lifecycle. Caller commits."""
    settings = settings or get_settings()
    postal = settings.postal_address or "Certuma, 1 Main St, Austin TX 78701"
    domain = settings.cold_domain or "getcertuma.com"

    # campaigns: a mix of autonomy levels, all active
    for name, autonomy in (("dermatology", "autonomous"), ("cardiology", "supervised"),
                           ("primary-care", "assisted")):
        session.execute(update(Campaign).where(Campaign.name == name)
                        .values(is_active=True, is_paused=False, autonomy_level=autonomy))
    # three approved A/B variants on the main campaign
    session.execute(delete(Template).where(Template.campaign == "dermatology"))
    for i, label in enumerate(VARIANTS, start=1):
        session.add(Template(campaign="dermatology", version=i,
                             subject={"A": "Your profile is ready", "B": "A quick note on your profile",
                                      "C": "Claim your Certumalink profile"}[label],
                             body=_BODY, variant_label=label, is_approved=True, approved_by="seed"))
    # a warmed mailbox (reuse if one already exists, so the seed is re-runnable)
    session.execute(update(Mailbox).values(is_active=False))
    mbx = session.execute(select(Mailbox).where(Mailbox.address == "jordan@getcertuma.com")).scalar()
    if mbx is None:
        session.add(Mailbox(address="jordan@getcertuma.com", domain=domain, is_active=True))
    else:
        mbx.is_active = True
    session.flush()

    counts: dict = {}
    support_candidates: list = []
    for i in range(n):
        npi = f"30{i:08d}"
        spec = SPECIALTIES[i % len(SPECIALTIES)]
        st = _STATES[i % len(_STATES)]
        city = _CITIES[i % len(_CITIES)]
        first, last = _FIRST[i % len(_FIRST)], _LAST[(i // len(_FIRST)) % len(_LAST)]
        group = 1 + (_pct(npi, "grp") % 15)
        campaign = ("dermatology" if spec == "Dermatology" else
                    "cardiology" if spec == "Cardiology" else "dermatology")
        variant = assign_variant(VARIANTS, npi)

        # clean prior + base rows (signals/events/etc. before the prospect they FK to)
        for tbl in (ClinicianSignal, SupportTicket, Event, Message, Contact, WorkflowScore):
            session.execute(delete(tbl).where(tbl.npi == npi))
        session.execute(delete(Approval).where(Approval.lead_id.in_(select(Lead.id).where(Lead.npi == npi))))
        session.execute(delete(Thread).where(Thread.lead_id.in_(select(Lead.id).where(Lead.npi == npi))))
        session.execute(delete(Lead).where(Lead.npi == npi))
        session.execute(delete(Prospect).where(Prospect.npi == npi))
        session.execute(delete(PracticeGroup).where(PracticeGroup.practice_group_id == f"ag{npi}"))
        session.add(PracticeGroup(practice_group_id=f"ag{npi}", practice_group_size=group))
        session.flush()
        tier = "high" if group >= 9 else "medium" if group >= 4 else "low"
        session.add(Prospect(npi=npi, first_name=first, last_name=last, display_name=f"{first} {last} MD",
                             credential="MD", primary_specialty=spec, practice_city=city, practice_state=st,
                             practice_group_id=f"ag{npi}"))
        session.flush()
        session.add(WorkflowScore(npi=npi, campaign="", activation_priority=tier, activation_score=70,
                                  profile_completeness_score=90, practice_group_size=group, model_version="seed"))

        phase = i % 100
        status, has_contact, sent = _classify_phase(npi, variant, phase)
        if has_contact:
            session.add(Contact(npi=npi, email=f"{first.lower()}.{last.lower()}{i}@example.com",
                                email_status="valid", discovery_source="seed"))
        lead = Lead(npi=npi, campaign=campaign, activation_status=status,
                    claim_url=f"https://www.certumalink.com/claim/{npi}",
                    cadence_step=(1 if sent else 0))
        session.add(lead)
        session.flush()
        counts[status] = counts.get(status, 0) + 1

        # some sendable leads are awaiting a human send-approval (the Approvals queue)
        if status == "sendable" and _pct(npi, "appr") < 55:
            body = (f"Hi Dr. {last}, your {spec} practice in {city}. Review your profile: "
                    f"{lead.claim_url}. Unsubscribe: https://{domain}/u/{npi}. {postal}")
            session.add(Approval(lead_id=lead.id, proposed_action="send_email", value_tier=tier,
                                proposed_subject="Your Certumalink profile is ready",
                                proposed_body=body, state="pending"))
            counts["pending_send_approvals"] = counts.get("pending_send_approvals", 0) + 1

        if sent:
            _seed_sent_lead(session, lead, npi, variant, status, when, postal, domain, campaign, i)

        # activated physicians chat with support; a slice of still-onboarding leads ask setup questions
        if status == "physician_activated":
            support_candidates.append((npi, "activated"))
        elif sent and status in ("email_sent", "interested", "awaiting_reply") and _pct(npi, "sup") < 30:
            support_candidates.append((npi, "onboarding"))

    signals.run_signal_collection(session, when=when, limit=10_000)
    tickets, summary = _seed_support(session, support_candidates, when)
    counts["support_tickets"] = tickets
    counts["support_signals"] = summary.signals_emitted
    counts["support_escalations"] = summary.escalated
    reporting.rebuild(session, as_of=when)
    counts["total"] = n
    return counts


# (subject, body) keyed to the intent the stub classifier will infer from the body keywords.
# Order matters: indices 6 and 7 are the onboarding/how-to messages used for still-onboarding leads.
_SUPPORT_MSGS = [
    ("Can we add more seats?",
     "We love this so far - can you add more seats for the other three providers in our group? We "
     "want to expand to our whole practice."),
    ("Thank you - this is fantastic",
     "Just wanted to say I love it. The profile claim flow was so helpful, best onboarding I have had. "
     "Thank you so much."),
    ("Frustrated - considering canceling",
     "I am frustrated and disappointed. If this cannot be sorted out I want to cancel and get a refund."),
    ("Profile page is broken",
     "There is a bug - my profile page is not loading and shows a 404 error. It does not work at all."),
    ("Any plans to integrate with our EHR?",
     "Would be great if you could integrate with our EHR. Any plans to add that feature?"),
    ("Question about my invoice",
     "I have a billing question about the latest charge on my subscription invoice - can you help?"),
    ("Help claiming my profile",
     "I need help to finish setup - where is my claim link so I can activate my profile?"),
    ("How do I edit my listing?",
     "How do I edit the specialties and hours shown on my listing?"),
]


def _seed_support(session, candidates, when):
    """Create inbound support tickets for the candidates, then run the support pass to classify,
    answer/escalate, and emit support-derived sales signals into the knowledge graph."""
    created = 0
    for npi, kind in candidates:
        if kind == "activated":
            idx = _pct(npi, "supmsg") % len(_SUPPORT_MSGS)        # full spread of intents
        else:
            idx = 6 + (_pct(npi, "supmsg") % 2)                   # onboarding_help / how_to
        subject, body = _SUPPORT_MSGS[idx]
        created_at = when - timedelta(days=(_pct(npi, "supage") % 12), hours=(_pct(npi, "suph") % 24))
        session.add(SupportTicket(npi=npi, channel=("portal" if idx % 2 else "email"),
                                  subject=subject, body=body, status="open", created_at=created_at))
        created += 1
    session.flush()
    summary = support.run_support(session, when=when, limit=10_000)
    return created, summary


def _classify_phase(npi, variant, phase):
    """-> (status, has_contact, was_sent)."""
    if phase < 8:
        return "not_contacted", False, False
    if phase < 14:
        return "enriching", False, False
    if phase < 24:
        return "sendable", True, False
    # the rest were sent: variant-weighted activation, then a spread of outcomes
    if _pct(npi, "act") < _ACT_RATE[variant]:
        return "physician_activated", True, True
    sub = _pct(npi, "sub")
    if sub < 9:
        return "needs_review", True, True       # objection -> escalation
    if sub < 18:
        return "do_not_contact", True, True      # opted out
    if sub < 26:
        return "exhausted", True, True
    if sub < 34:
        return "interested", True, True
    if sub < 52:
        return "email_sent", True, True
    return "awaiting_reply", True, True


def _seed_sent_lead(session, lead, npi, variant, status, when, postal, domain, campaign, i):
    thread = Thread(lead_id=lead.id, reply_token=f"tok{npi}")
    session.add(thread)
    session.flush()
    sent_at = when - timedelta(days=2 + (_pct(npi, "age") % 18))
    channel = "linkedin" if (_pct(npi, "chan") < 12) else "email"
    out = Message(lead_id=lead.id, thread_id=thread.id, npi=npi, campaign=campaign, cadence_step=0,
                  direction="outbound", channel=channel, variant_id=variant, subject="Your profile",
                  body_rendered="...", esp_message_id=f"o{npi}", sent_at=sent_at,
                  delivered=(_pct(npi, "del") < 92), bounced=(_pct(npi, "del") >= 92))
    session.add(out)
    session.flush()

    # opens (engagement) - varied recency drives opened-no-reply / went-quiet / churn
    if _pct(npi, "open") < 62:
        last_open = when - timedelta(days=(_pct(npi, "openage") % 20))
        lead.open_count = 1 + (_pct(npi, "opens") % 3)
        lead.last_open_at = last_open
        lead.last_engaged_at = last_open
        session.add(Event(dedup_key=f"op{npi}", lead_id=lead.id, npi=npi, event_type="opened",
                          occurred_at=last_open))
    if status in ("awaiting_reply", "interested", "email_sent"):
        lead.next_action_at = when - timedelta(days=1)  # due, so cadence/engagement views light up

    if status == "physician_activated":
        lead.activation_detected_at = when - timedelta(days=(_pct(npi, "actage") % 10))
        session.add(Event(dedup_key=f"ev{npi}", lead_id=lead.id, npi=npi, event_type="activated",
                          occurred_at=lead.activation_detected_at))
    elif status == "do_not_contact":
        session.add(Suppression(npi=npi, reason="opt_out", source="unsubscribe_click"))
        session.add(Message(lead_id=lead.id, npi=npi, campaign=campaign, cadence_step=0, direction="inbound",
                           channel="email", body_rendered="please unsubscribe", esp_message_id=f"i{npi}",
                           reply_classification="unsubscribe"))
    elif status == "needs_review":
        session.add(Message(lead_id=lead.id, npi=npi, campaign=campaign, cadence_step=0, direction="inbound",
                           channel="email", body_rendered="how much does this cost? is this legit?",
                           esp_message_id=f"i{npi}", reply_classification="objection"))
        body = (f"Hi Dr., thanks for the reply. Happy to clarify - no cost, no obligation. "
                f"Review your profile: {lead.claim_url}. Unsubscribe: https://{domain}/u/{npi}. {postal}")
        session.add(Approval(lead_id=lead.id, proposed_action="reply", gate_reason_code="objection",
                            proposed_subject="Re: your profile", proposed_body=body, state="pending"))
    elif status == "interested":
        session.add(Message(lead_id=lead.id, npi=npi, campaign=campaign, cadence_step=0, direction="inbound",
                           channel="email", body_rendered="yes, interested - send me the link",
                           esp_message_id=f"i{npi}", reply_classification="interested"))
    session.flush()


def main(argv=None) -> int:
    from certuma.db.session import make_engine
    settings = get_settings()
    settings = Settings(**{**settings.__dict__,
                           "postal_address": settings.postal_address or "Certuma, 1 Main St, Austin TX 78701",
                           "cold_domain": settings.cold_domain or "getcertuma.com"})
    engine = make_engine(settings)
    with Session(engine) as session:
        counts = seed_active(session, settings=settings)
        session.commit()
    print("=== Certuma Reach active-campaign seed ===")
    for k in sorted(counts):
        print(f"  {k:20}: {counts[k]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
