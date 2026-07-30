"""RFI classifier (WS6 — Phase 3 enabled).

Backend selection (env CLASSIFIER_BACKEND): "llm" (default when a key exists)
runs llm.classify_request per confirmed inbound; "stub" keeps v1 behavior.
Stated urgency is set ONCE here at creation — never re-graded later (spec 6.1);
escalation is deterministic and computed at render time (requests_logic.py).
"""
import os
from dataclasses import dataclass

from . import llm


@dataclass
class RfiResult:
    is_info_request: bool = False
    category: str | None = None      # key into Choices.req_category
    confidence: int = 0
    description: str = ""            # one LLM-extracted sentence
    urgency: str = "none"            # none | urgent_language | explicit_deadline
    deadline: str | None = None      # ISO date when explicit_deadline


def _backend() -> str:
    b = os.environ.get("CLASSIFIER_BACKEND")
    if b:
        return b
    try:
        from .config import load_env
        b = load_env().get("CLASSIFIER_BACKEND")
    except Exception:
        b = None
    return b or ("llm" if llm.available() else "stub")


def classify(subject: str, text: str, backend: str | None = None) -> RfiResult:
    """`text` is the transient enriched body (in memory only)."""
    backend = backend or _backend()
    if backend == "stub" or not text:
        return RfiResult()
    data = llm.classify_request(subject, text)
    if not data or not data.get("is_request"):
        return RfiResult()
    return RfiResult(
        is_info_request=True,
        category=data.get("category") or "Other",
        confidence=80,
        description=(data.get("description") or subject)[:200],
        urgency=data.get("urgency") or "none",
        deadline=data.get("deadline"),
    )
