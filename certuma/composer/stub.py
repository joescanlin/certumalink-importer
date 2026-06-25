"""Deterministic template composer (Studio AI compose) - no LLM.

Produces realistic, compliant copy per message type so the studio is fully usable (and testable)
without an API key. Every body keeps the three literal compliance tokens and avoids the banned
claims, so the output lints clean and can be approved straight into the A/B set. The body keeps a
{last_name} personalization token for the copywriter to fill per lead; {specialty} is substituted at
compose time from the request.
"""
from __future__ import annotations

from .provider import ComposeOutput, ComposeRequest

__all__ = ["StubComposeProvider"]

_FOOTER = "\n\nUnsubscribe any time: {unsubscribe_url}\n{postal_address}"

# message_type -> (subject, the lead-in lines before the shared claim line + footer)
_TEMPLATES = {
    "first_touch": (
        "Your {specialty} profile on Certumalink is ready",
        "Hi Dr. {last_name},\n\nWe put together a Certumalink profile for your {specialty} practice so "
        "patients searching locally can find you. It is a draft we prepared for you to review, not an "
        "endorsement.\n\nReview and claim it here: {claim_url}.",
    ),
    "follow_up_1": (
        "A quick follow-up on your Certumalink profile",
        "Hi Dr. {last_name},\n\nCircling back in case my last note got buried. Your {specialty} profile "
        "is still reserved and takes about two minutes to claim.\n\nClaim it here: {claim_url}.",
    ),
    "follow_up_2": (
        "Last note about your Certumalink profile",
        "Hi Dr. {last_name},\n\nI will stop here so I am not a bother. If a stronger local presence for "
        "your {specialty} practice is useful, your profile is one click away.\n\nClaim it here: {claim_url}.",
    ),
    "objection_reply": (
        "Re: your Certumalink profile",
        "Hi Dr. {last_name},\n\nGreat question, and thanks for the reply. There is no cost and no "
        "obligation - this is simply a profile we drafted so local patients can find your {specialty} "
        "practice. You stay in full control of it.\n\nReview it here: {claim_url}.",
    ),
    "re_engage": (
        "Still keeping your Certumalink profile reserved",
        "Hi Dr. {last_name},\n\nIt has been a little while, so a friendly nudge: your {specialty} "
        "profile is still held for you and ready whenever you are.\n\nPick it back up here: {claim_url}.",
    ),
}


class StubComposeProvider:
    name = "stub"

    def compose(self, req: ComposeRequest) -> ComposeOutput:
        subject, lead = _TEMPLATES.get(req.message_type, _TEMPLATES["first_touch"])
        specialty = (req.specialty or "practice").strip()
        subject = subject.replace("{specialty}", specialty)
        body = lead.replace("{specialty}", specialty)
        brief = (req.brief or "").strip()
        if brief:
            # fold the brief in as an angle line, kept plain so it never trips the compliance lint
            body += f"\n\n{brief.rstrip('.')}."
        body += _FOOTER
        return ComposeOutput(subject=subject, body=body)
