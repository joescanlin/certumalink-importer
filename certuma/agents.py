"""Agent registry / prompt store (Agent Studio, Phase 2).

The LLM agents (copywriter, reply classifier, reply drafter) have system prompts that should be
tunable by the sales lead without a deploy. This stores their configs (role, model, prompt) in the
agent table, with one ACTIVE row per role; the real providers load the active prompt and fall back
to their in-code default when none exists. Editing a prompt updates the row (and bumps its version);
"spinning up a fresh agent" inserts a new row for a role; activating one switches which config the
live provider uses. The deterministic nodes (Gate, Sender, Monitor, Poller, Orchestrator) are not
agents in this sense - they have fixed roles and no prompt - but they appear in the workflow view.

Caller owns the transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from certuma.classifier.anthropic_provider import HAIKU
from certuma.classifier.anthropic_provider import SYSTEM_PROMPT as CLASSIFIER_PROMPT
from certuma.copywriter.node import OPUS, SONNET
from certuma.copywriter.anthropic_provider import SYSTEM_PROMPT as COPYWRITER_PROMPT
from certuma.db.models import Agent
from certuma.observability import METRICS, emit, get_logger

__all__ = [
    "ROLES", "ROLE_LABELS", "DEFAULTS", "ensure_seeded", "list_agents", "get_active",
    "active_prompt", "create_agent", "update_agent", "activate_agent",
    "build_copy_provider", "build_classifier",
]

_LOG = get_logger("certuma.agents")

ROLES = ("copywriter", "classifier", "reply_drafter")
ROLE_LABELS = {
    "copywriter": "Copywriter",
    "classifier": "Reply Classifier",
    "reply_drafter": "Reply Drafter",
}

_REPLY_DRAFTER_PROMPT = (
    "You are Certuma, drafting a short, professional reply to a physician who answered our outreach "
    "with a question or an objection. Address their point directly and honestly; never claim the "
    "physician is verified, board-certified, or endorsed; keep the claim-link and unsubscribe "
    "footer intact. This draft is always reviewed by a human before it is sent."
)


@dataclass(frozen=True)
class _Default:
    name: str
    model: str
    prompt: str


DEFAULTS = {
    "copywriter": _Default("Default copywriter", SONNET, COPYWRITER_PROMPT),
    "classifier": _Default("Default reply classifier", HAIKU, CLASSIFIER_PROMPT),
    "reply_drafter": _Default("Default reply drafter", OPUS, _REPLY_DRAFTER_PROMPT),
}


def ensure_seeded(session: Session) -> None:
    """Insert the default active agent for any role that has none. Idempotent."""
    present = set(session.execute(select(Agent.role)).scalars().all())
    for role in ROLES:
        if role in present:
            continue
        d = DEFAULTS[role]
        session.add(Agent(role=role, name=d.name, model=d.model, system_prompt=d.prompt,
                          is_active=True, version=1, created_by="seed"))
    session.flush()


def list_agents(session: Session) -> List[Agent]:
    return list(session.execute(
        select(Agent).order_by(Agent.role, Agent.is_active.desc(), Agent.id)
    ).scalars().all())


def get_active(session: Session, role: str) -> Optional[Agent]:
    return session.execute(
        select(Agent).where(Agent.role == role, Agent.is_active.is_(True)).limit(1)
    ).scalar()


def active_prompt(session: Session, role: str) -> str:
    """The active prompt for a role, or the in-code default if none is configured."""
    agent = get_active(session, role)
    if agent is not None:
        return agent.system_prompt
    return DEFAULTS[role].prompt if role in DEFAULTS else ""


def create_agent(session: Session, *, role: str, name: str, model: str, system_prompt: str,
                 activate: bool = False, created_by: str = "console") -> Agent:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    if not name.strip() or not system_prompt.strip():
        raise ValueError("name and system_prompt are required")
    agent = Agent(role=role, name=name.strip(), model=model.strip(),
                  system_prompt=system_prompt, is_active=False, version=1, created_by=created_by)
    session.add(agent)
    session.flush()
    if activate:
        activate_agent(session, agent.id)
    METRICS.incr("agent_created", role=role)
    emit(_LOG, "agent_created", agent_id=agent.id, role=role, name=name)
    return agent


def update_agent(session: Session, agent_id: int, *, name: Optional[str] = None,
                 model: Optional[str] = None, system_prompt: Optional[str] = None) -> Agent:
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise KeyError(agent_id)
    if name is not None and name.strip():
        agent.name = name.strip()
    if model is not None:
        agent.model = model.strip()
    if system_prompt is not None and system_prompt.strip():
        agent.system_prompt = system_prompt
        agent.version += 1
    session.flush()
    METRICS.incr("agent_updated", role=agent.role)
    emit(_LOG, "agent_updated", agent_id=agent.id, role=agent.role, version=agent.version)
    return agent


def activate_agent(session: Session, agent_id: int) -> Agent:
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise KeyError(agent_id)
    # deactivate the current active row for this role BEFORE activating (partial unique index)
    session.execute(
        update(Agent).where(Agent.role == agent.role, Agent.is_active.is_(True)).values(is_active=False)
    )
    agent.is_active = True
    session.flush()
    METRICS.incr("agent_activated", role=agent.role)
    emit(_LOG, "agent_activated", agent_id=agent.id, role=agent.role)
    return agent


def build_copy_provider(session: Session, api_key: str):
    """The real copywriter provider wired to the active prompt (integration seam)."""
    from certuma.copywriter import AnthropicCopyProvider
    return AnthropicCopyProvider(api_key, system=active_prompt(session, "copywriter"))


def build_classifier(session: Session, api_key: str):
    """The real reply classifier wired to the active prompt (integration seam)."""
    from certuma.classifier import AnthropicReplyClassifier
    active = get_active(session, "classifier")
    model = active.model if active else HAIKU
    return AnthropicReplyClassifier(api_key, model=model, system=active_prompt(session, "classifier"))
