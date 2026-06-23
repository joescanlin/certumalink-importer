"""Outbound message assembly (Phase 1 task P1.2).

build_outbound sets the RFC 8058 one-click List-Unsubscribe headers. to_mime renders the
multipart MIME (text + html) with From display name and plus-addressed Reply-To. The body must
already contain the rendered unsubscribe link, postal address, and claim_url (rendered and
pre-linted upstream). This layer does not invent or re-validate body content; the SENDER applies
a cheap last-line presence guard.
"""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .provider import OutboundEmail

__all__ = ["build_outbound", "to_mime"]


def build_outbound(
    *,
    to_addr: str,
    from_addr: str,
    from_name: str,
    subject: str,
    html_body: str,
    text_body: str,
    reply_to: str,
    unsubscribe_url: str,
    unsubscribe_mailto: str,
) -> OutboundEmail:
    """Assemble an OutboundEmail with the one-click List-Unsubscribe headers."""
    if not unsubscribe_url or not unsubscribe_mailto:
        raise ValueError("both unsubscribe_url and unsubscribe_mailto are required (CAN-SPAM)")
    headers = {
        "List-Unsubscribe": f"<{unsubscribe_mailto}>, <{unsubscribe_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    return OutboundEmail(
        to_addr=to_addr, from_addr=from_addr, from_name=from_name, subject=subject,
        html_body=html_body, text_body=text_body, reply_to=reply_to, headers=headers,
    )


def to_mime(email: OutboundEmail, *, domain: str = "certuma") -> EmailMessage:
    """Render an OutboundEmail to a multipart MIME message with a stable Message-ID."""
    msg = EmailMessage()
    msg["From"] = formataddr((email.from_name, email.from_addr))
    msg["To"] = email.to_addr
    msg["Subject"] = email.subject
    if email.reply_to:
        msg["Reply-To"] = email.reply_to
    msg["Message-ID"] = make_msgid(domain=domain)
    for key, value in email.headers.items():
        msg[key] = value
    msg.set_content(email.text_body or "")
    msg.add_alternative(email.html_body or "", subtype="html")
    return msg
