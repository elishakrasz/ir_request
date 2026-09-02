"""
DRAFT — NOT WIRED.  Phase-0 reference for claude_ir.md §4.1.
=============================================================
Shows how the classifier should write category as a RECOMMENDATION against the
versioned v3 taxonomy (new_ircategory), per the brief:
  - AI writes new_category_recommended / _confidence / _provenance
  - a HUMAN promotes to new_category_actual (never from an automated path)
  - reclassification is idempotent and never touches _actual
This file lives in docs/phase0/ so it is never imported by the ingestion
package. When Phase 0 lands, fold the relevant pieces into ingestion/classify.py
+ ingestion/llm.py as a single reviewed PR.
"""
from __future__ import annotations
import hashlib
import json

# The taxonomy is data, not code: in production load the ACTIVE rows from the
# new_ircategory reference table (code + definition) so the list changes without
# a deploy. This constant mirrors the v3 seed only as a fallback / for tests.
PROMPT_VERSION = "ir-cat-v3-2026-09-01"

V3_FALLBACK = [
    ("Reporting", "Fund/investment reports, quarterly reports, financial statements, audit support."),
    ("Distribution", "Firm-initiated payouts: in-kind (SpaceX) & cash distributions, DTC/brokerage delivery, eligibility."),
    ("CapitalCall", "Capital-call notices, wire/payment confirmations, receipt queries, management fees."),
    ("SubscriptionDocs", "Subscription/onboarding paperwork, commitments, signature support, custody feedback."),
    ("TaxDocs", "K-1s, tax information, W-8/W-9, tax-document timelines."),
    ("AccountAdmin", "Portal access, address/email/contact changes, distribution-list, own-entity transfers."),
    ("CartaOnboarding", "Getting onto/through Carta: activation, portal access, e-signature problems."),
    ("Meeting", "Scheduling calls, intros, business-update invitations, networking."),
    ("CapitalAccount", "Per-investor capital-account statements: requests, resends, discrepancies."),
    ("Valuation", "Current valuation, NAV, price-per-share, carry estimates, portfolio value."),
    ("CoInvestment", "Co-invest / follow-on opportunity notices and investor elections."),
    ("KYC-AML", "Identity documents, outstanding KYC, FATCA/CRS, compliance verification."),
    ("LiquidityTransfer", "Investor-initiated secondary transfers or redemptions of an existing interest."),
    ("Legal", "Side letters, consents, NDAs, other legal terms."),
    ("Other", "Genuinely uncategorised investor mail (noise is dropped upstream, not filed here)."),
]


def load_active_categories(dv=None):
    """Production: SELECT code, definition FROM new_ircategory WHERE active=true
    ORDER BY sortorder. Falls back to the v3 constant when no client given."""
    if dv is None:
        return list(V3_FALLBACK)
    rows = dv.query("new_ircategories?$select=new_code,new_definition"
                    "&$filter=new_active eq true&$orderby=new_sortorder")
    return [(r["new_code"], r.get("new_definition") or "") for r in rows]


def build_system(cats) -> str:
    lines = "\n".join(f"- {code}: {defn}" for code, defn in cats)
    return (
        "You categorise an inbound investor email for a private-equity IR desk. "
        "Choose the CLOSEST category from this fixed, versioned list and rate your "
        "confidence 0-100. If nothing fits, use Other with low confidence.\n\n"
        f"Categories:\n{lines}\n\n"
        "Return only the category code and a 0-100 confidence. Do NOT invent a "
        "category outside the list."
    )


def schema(cats) -> dict:
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": [c for c, _ in cats]},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        },
        "required": ["category", "confidence"],
        "additionalProperties": False,
    }


def classify_recommended(subject, body, cats, client, model):
    """Returns (category_code, confidence). Deterministic figures/labels only —
    the model picks a code, never free-text."""
    resp = client.messages.create(
        model=model, max_tokens=256, system=build_system(cats),
        messages=[{"role": "user", "content": f"Subject: {subject}\n\n{body[:4000]}"}],
        output_config={"format": {"type": "json_schema", "schema": schema(cats)}})
    if resp.stop_reason == "refusal":
        return "Other", 0
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    return data["category"], int(data["confidence"])


def provenance(model, signal_ids, ts_iso) -> str:
    """One compact JSON string for new_*_provenance (satisfies §2)."""
    return json.dumps({"model": model, "prompt_ver": PROMPT_VERSION,
                       "ts": ts_iso, "signal_ids": list(signal_ids)},
                      separators=(",", ":"))


def recommendation_patch(prefix, cat_code, confidence, model, signal_ids, ts_iso,
                         category_rowid):
    """The PATCH body for new_inforequest. Writes ONLY the recommendation fields
    — never new_category_actual. `category_rowid` is the new_ircategory row GUID
    resolved from cat_code (lookup)."""
    p = prefix
    return {
        f"{p}category_recommended@odata.bind": f"/new_ircategories({category_rowid})",
        f"{p}category_confidence": confidence,
        f"{p}category_provenance": provenance(model, signal_ids, ts_iso),
    }


# ── idempotency / reclassification (§4.1) ────────────────────────────────────
#  * Cache key = sha256(prompt_ver + sorted(signal_ids) + subject + body).
#    Same input + same taxonomy version  ->  no re-call, no write.
#  * A reclassification run on a NEW category version updates _recommended only.
#  * NEVER write _actual from here. A user action copies _recommended -> _actual
#    (or picks another) and stamps the audit row.
#  * A record whose _actual is already set by a human keeps it; the rerun may
#    still refresh _recommended (shown as "AI now suggests X" next to the human
#    value), but must not overwrite _actual.
def cache_key(signal_ids, subject, body) -> str:
    basis = PROMPT_VERSION + "|" + ",".join(sorted(signal_ids)) + "|" + subject + "|" + body
    return hashlib.sha256(basis.encode()).hexdigest()[:32]
