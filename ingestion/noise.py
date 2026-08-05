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


def _bulk_sender(sender: str, rules: Rules) -> str | None:
    """Tier-0 admin/platform blast senders (2-yr ir@ analysis: ~52% of real
    inbound). Exact address first, then domain suffix. Named fund-admin staff
    (e.g. a person @apexgroup.com) are NOT listed — only the robot addresses."""
    s = (sender or "").lower()
    if s in (a.lower() for a in rules.bulk_senders):
        return s
    d = domain_of(s)
    for blocked in rules.bulk_sender_domains:
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
    # Tier-0 admin blasts — checked before the matched-contact shortcut on
    # purpose: a robot address that happens to be a CRM contact is still a blast
    blast = _bulk_sender(sender, rules)
    if blast:
        return "noise", f"admin_blast:{blast}"
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
