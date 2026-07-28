"""Pure matching logic: direction, exclusions, auto-reply detection,
participant → contact matching, hierarchical opportunity resolution.
No I/O — fully unit-testable (spec Phase 2 steps 4-5).
"""
import re
from datetime import datetime

from .config import Rules


def text_hit(needle: str, text: str) -> bool:
    """Word-boundary containment — 'Fund II' must NOT match inside 'Fund III'."""
    return bool(needle) and re.search(rf"\b{re.escape(needle.lower())}\b", text) is not None


def addr_of(recipient: dict) -> str:
    """Extract the lowercased SMTP address from a Graph recipient/from dict."""
    return (recipient or {}).get("emailAddress", {}).get("address", "").lower()


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def classify_direction(sender: str, recipients: list[str], org_domains: list[str]) -> str:
    """Three-way (spec step 4): external sender → Inbound; internal sender with
    ≥1 external recipient (to/cc/bcc) → Outbound; all-internal → Internal.
    Internal must never register as Outbound or it poisons response latency."""
    if domain_of(sender) not in org_domains:
        return "Inbound"
    if any(domain_of(r) not in org_domains for r in recipients):
        return "Outbound"
    return "Internal"


def is_excluded(sender: str, subject: str, snippet: str, rules: Rules) -> bool:
    s = sender.lower()
    if s in (x.lower() for x in rules.excluded_senders):
        return True
    if domain_of(s) in (x.lower() for x in rules.excluded_domains):
        return True
    text = f"{subject} {snippet}".lower()
    return any(k.lower() in text for k in rules.excluded_keywords)


def looks_autoreply(subject: str, sender: str, rules: Rules) -> bool:
    """Preliminary auto-reply/bounce/mass-mail detection from delta-level
    evidence; finalized against real headers in the enrichment step."""
    subj = (subject or "").lower().strip()
    if any(subj.startswith(p.lower()) for p in rules.autoreply_subject_prefixes):
        return True
    return any(p.lower() in sender.lower() for p in rules.autoreply_sender_patterns)


def headers_autoreply(headers: list[dict]) -> bool:
    """Final check from real internetMessageHeaders (enrichment step)."""
    for h in headers or []:
        name = (h.get("name") or "").lower()
        value = (h.get("value") or "").lower()
        if name == "auto-submitted" and value and value != "no":
            return True
        if name == "precedence" and value in ("bulk", "list", "junk"):
            return True
        if name in ("x-autoreply", "x-autorespond"):
            return True
    return False


def match_contacts(participants: list[str], email_map: dict, org_domains: list[str]) -> dict:
    """external participant addresses → {contactid: set(opportunityids)}.
    Exact address match only — plus-addressed variants deliberately unmatched."""
    matched: dict = {}
    for p in participants:
        p = p.lower()
        if domain_of(p) in org_domains:
            continue
        hit = email_map.get(p)
        if hit:
            cid, oppids = hit
            matched.setdefault(cid, set()).update(oppids)
    return matched


def resolve_opportunity(msg_time: datetime, subject: str, snippet: str,
                        contact_oppids: set, opp_meta: dict, conv_opp: str | None):
    """Hierarchical, confidence-scored (spec step 5). Returns
    (oppid|None, method|None, confidence, status). Never silently assigns at
    low confidence — Suggested/Unmatched land in the review queue."""
    text = f"{subject} {snippet}".lower()

    # 1. Explicit — oppcode in subject/snippet
    for oid, m in opp_meta.items():
        if text_hit(m.get("oppcode") or "", text):
            return oid, "Explicit", 97, "Confirmed"

    # 2. Thread inheritance — conversation already Confirmed
    if conv_opp:
        return conv_opp, "Thread", 90, "Confirmed"

    # 3. Contact match — exactly one live, actively-monitored opp postdating start
    candidates = [
        oid for oid in contact_oppids
        if (m := opp_meta.get(oid))
        and m.get("active")
        and (m.get("startdate") is None or msg_time >= m["startdate"])
    ]
    if len(candidates) == 1:
        return candidates[0], "ContactMatch", 75, "Confirmed"

    # 4. Content — alias narrows multiple candidates to exactly one
    pool = candidates or list(contact_oppids)
    hits = [oid for oid in pool
            if any(text_hit(a, text) for a in opp_meta.get(oid, {}).get("aliases", []))]
    if len(hits) == 1:
        return hits[0], "Content", 60, "Suggested"

    # 5. Suggested (top candidate recorded) or Unmatched
    if pool:
        return pool[0], None, 30, "Suggested"
    return None, None, 0, "Unmatched"
