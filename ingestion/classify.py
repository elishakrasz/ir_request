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
    routing: str | None = None       # §2.2 4-way routing (Choices.routing key)
    secondary: str | None = None     # second topic when clearly present (v2 taxonomy)
    third_party: bool = False        # sender acts for an investor (CPA/advisor/custodian)
    confidence: int = 0              # classifier's own 0-100 (v2: from the LLM)
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
    # snake_case LLM label → Choices.routing key (None when absent, e.g. from
    # cache entries that predate the routing field)
    routing_key = {"process_blocker": "ProcessBlocker",
                   "conviction": "Conviction",
                   "deal_mechanics": "DealMechanics",
                   "scheduling": "Scheduling"}.get(data.get("routing_category"))
    return RfiResult(
        is_info_request=True,
        category=data.get("category") or "Other",
        routing=routing_key,
        secondary=data.get("secondary_category"),
        third_party=bool(data.get("third_party")),
        confidence=min(100, max(0, int(data.get("confidence") or 80))),
        description=(data.get("description") or subject)[:200],
        urgency=data.get("urgency") or "none",
        deadline=data.get("deadline"),
    )
