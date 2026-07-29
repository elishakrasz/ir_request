"""WS1 noise gate — runs BEFORE matching. Pure/deterministic (LLM fallback
lives in llm.py; callers combine the two).

Three-way outcome per message:
  ("noise", reason)   — definitely noise; kept in the signal table with
                        disposition Excluded + new_noisereason, out of
                        matching/rollups/timeline/counts
  ("candidate", None) — heuristics can't decide → LLM fallback
  ("ok", None)        — proceed to matching
"""
from .config import Rules
from .matching import domain_of


def _domain_blocked(sender: str, rules: Rules) -> str | None:
    d = domain_of(sender)
    for blocked in rules.noise_domains:
        b = blocked.lower()
        if d == b or d.endswith("." + b):
            return b
    return None


def header_noise(headers: list[dict]) -> str | None:
    """List/bulk header evidence (enrichment-time or probe-time only — delta
    payloads carry no headers; stored signals carry none either)."""
    for h in headers or []:
        name = (h.get("name") or "").lower()
        value = (h.get("value") or "").lower()
        if name in ("list-unsubscribe", "list-id"):
            return "list_headers"
        if name == "precedence" and value in ("bulk", "list", "junk"):
            return "bulk_precedence"
        if name == "auto-submitted" and value and value != "no":
            return "auto_submitted"
    return None


def gate(sender: str, subject: str, snippet: str, rules: Rules, *,
         headers: list[dict] | None = None,
         sender_is_matched_contact: bool = False,
         sender_is_internal: bool = False) -> tuple[str, str | None]:
    """Deterministic heuristics, in order. No LLM cost here."""
    h = header_noise(headers)
    if h:
        return "noise", h
    b = _domain_blocked(sender, rules)
    if b:
        return "noise", f"blocklist:{b}"
    # An external sender who is neither a known contact nor internal, whose mail
    # only matched via recipients (the WSJ-cc case) → LLM decides, never dropped
    # outright (spec 1.1).
    if not sender_is_matched_contact and not sender_is_internal:
        return "candidate", None
    return "ok", None


def llm_label_to_noise_reason(label: str) -> str | None:
    """Map an llm.classify_noise label to a noisereason (None = not noise)."""
    return {
        "newsletter_or_marketing": "llm:newsletter_or_marketing",
        "internal_ops": "llm:internal_ops",
        "other_noise": "llm:other_noise",
    }.get(label)
