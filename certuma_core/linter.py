"""Deterministic copy linter (Phase 1 task P1.6).

Validates the RENDERED email (post token-substitution) before any send. Pure. Checks, in order:
  1. no leftover unrendered {tokens};
  2. the exact claim_url, the unsubscribe URL, and the postal address are all present (CAN-SPAM);
  3. no banned claim (the data is uncredentialed);
  4. hallucination guard: every multi-word Capitalized proper noun traces, word-by-word, to the
     allow-list corpus (SeedFacts + pitch_angle + approved-template prose + sender identity) or a
     small set of safe common words. Single capitalized words (sentence starts) are not checked,
     to keep false-rejects low (see plan R3).

The guard is an ALLOW-list (trace-to-source), never a deny-list. A failure routes the lead to
human needs_review after one retry; it never silently sends.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .copy_schema import BANNED_CLAIMS

__all__ = ["LintResult", "lint"]

_LEFTOVER_TOKEN = re.compile(r"\{[a-z_]+\}")
# 2+ consecutive Capitalized/ALLCAPS words (likely a name/place/org); single caps are ignored
_PROPER_NOUN = re.compile(r"\b[A-Z][A-Za-z.'’-]*(?:\s+[A-Z][A-Za-z.'’-]*)+\b")
_WORD = re.compile(r"[a-z0-9']+")

# greetings, honorifics, pronouns, articles, and brand words that may appear capitalized
_SAFE_WORDS = frozenset({
    "dr", "mr", "ms", "mrs", "md", "do", "hi", "hello", "dear", "we", "i", "you", "your", "our",
    "the", "a", "an", "to", "from", "certuma", "certumalink", "reach",
})


@dataclass(frozen=True)
class LintResult:
    ok: bool
    violations: tuple = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return self.ok


def _allowed_words(sources: Iterable[str]) -> set:
    words = set(_SAFE_WORDS)
    for src in sources:
        words.update(_WORD.findall((src or "").lower()))
    return words


def lint(
    *,
    subject: str,
    body: str,
    plaintext: str,
    allowlist_sources: Iterable[str],
    claim_url: str,
    unsubscribe_url: str,
    postal_address: str,
    required_tokens: Iterable[str] = (),
) -> LintResult:
    violations = []
    parts = {"subject": subject or "", "body": body or "", "plaintext": plaintext or ""}
    blob = "\n".join(parts.values())
    blob_lower = blob.lower()

    # 1. no leftover unrendered tokens
    for name, text in parts.items():
        leftover = _LEFTOVER_TOKEN.findall(text)
        if leftover:
            violations.append(f"unrendered tokens in {name}: {sorted(set(leftover))}")

    # 2. compliance elements present (claim_url byte-exact, in both body and plaintext)
    if not claim_url or claim_url not in (body or "") or claim_url not in (plaintext or ""):
        violations.append("claim_url missing or altered (must appear exactly in body and plaintext)")
    if not unsubscribe_url or unsubscribe_url not in (body or ""):
        violations.append("unsubscribe url missing from body")
    if not postal_address or postal_address not in (body or ""):
        violations.append("postal address missing from body")

    # 3. banned claims
    for claim in BANNED_CLAIMS:
        if claim in blob_lower:
            violations.append(f"banned claim: {claim!r}")

    # 4. hallucination guard (word-level allow-list trace). The injected compliance values
    #    (postal address + URLs) are known-good, so their words are allowed too. Extraction is
    #    per-sentence so a sentence-ending word never chains to the next sentence's capital.
    allowed = _allowed_words(list(allowlist_sources) + [postal_address, unsubscribe_url, claim_url])
    seen = set()
    for text in parts.values():
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            for candidate in _PROPER_NOUN.findall(sentence):
                unknown = [w for w in _WORD.findall(candidate.lower()) if w not in allowed]
                if unknown:
                    msg = f"unverified proper noun {candidate!r} (unknown words: {unknown})"
                    if msg not in seen:
                        seen.add(msg)
                        violations.append(msg)

    return LintResult(ok=not violations, violations=tuple(violations))
