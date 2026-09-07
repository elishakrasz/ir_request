"""Central LLM configuration + the WS1 noise-classification fallback.

Spec ground rule: centralize model/prompt config; every LLM feature states and
honors a cost-control story.

COST CONTROLS (honored in code, not just stated):
1. Deterministic heuristics run FIRST (noise.py) — the LLM sees only candidates
   the heuristics could not decide.
2. Batched — up to BATCH_SIZE candidates per API call, structured-output JSON.
3. Cached — verdict per sha256(sender + subject) in state/llm_cache.json;
   repeat senders/subjects never re-invoke the LLM, across runs.
4. Cheap model — the spec calls for a "single cheap call" for noise triage, so
   this uses claude-haiku-4-5 ($1/$5 per MTok). Quality-sensitive features
   (WS5 summaries, WS6 request classification) use claude-opus-5.
5. Graceful degradation — no ANTHROPIC_API_KEY → available() is False and
   callers leave candidates in needs_review instead of guessing.
"""
import hashlib
import json
import os
from pathlib import Path

STATE = Path(__file__).resolve().parent / "state"
CACHE_FILE = STATE / "llm_cache.json"
BATCH_SIZE = 40

# Model config — env-overridable, one place (spec ground rule)
NOISE_MODEL = os.environ.get("NOISE_LLM_MODEL", "claude-haiku-4-5")
SUMMARY_MODEL = os.environ.get("SUMMARY_LLM_MODEL", "claude-opus-5")
REQUEST_MODEL = os.environ.get("REQUEST_LLM_MODEL", "claude-opus-5")

NOISE_LABELS = ["investor_correspondence", "newsletter_or_marketing",
                "internal_ops", "other_noise"]

NOISE_SYSTEM = (
    "You classify emails for a private-equity investor-relations team. "
    "Given sender, subject, and a snippet, label each item:\n"
    "- investor_correspondence: a real exchange with an investor/counterparty "
    "(questions, documents, meetings, deal or fund matters)\n"
    "- newsletter_or_marketing: news digests, market commentary, event promos, "
    "product marketing, mass mail\n"
    "- internal_ops: internal firm operations (IT, HR, payroll, facilities)\n"
    "- other_noise: automated notifications, receipts, alerts, anything else "
    "that is not human correspondence\n"
    "When genuinely uncertain, prefer investor_correspondence (false noise is "
    "worse than a review-queue item)."
)

NOISE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "label": {"type": "string", "enum": NOISE_LABELS},
                },
                "required": ["index", "label"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


# Category taxonomy v2 (2026-08-04, from the 2-year ir@ analysis in
# docs/ir-triage-categories.md). APPEND-ONLY — order mirrors the Dataverse
# local option set on new_inforequest.new_category / new_secondarycategory.
ROUTING_CATEGORIES = ["process_blocker", "conviction", "deal_mechanics",
                      "scheduling"]

REQUEST_CATEGORIES = [
    "Reporting", "CapitalAccount", "Valuation", "KYC-AML", "SubscriptionDocs",
    "Legal-SideLetter", "Meeting", "DataRoom", "Other",
    "CapitalCall", "TaxDocs", "AccountAdmin", "LiquidityTransfer",
    "NDA",  # v3 2026-08-06 (append-only)
    "CartaOnboarding", "BrokerageDetails",  # v4 2026-08-09 (append-only)
]

REQUEST_SYSTEM = (
    "You screen inbound investor emails for a private-equity IR team, "
    "detecting information requests that require a response or action.\n"
    "An information request = the sender asks the firm for something: a "
    "document, a figure, a signature, a meeting, access, a status update, an "
    "answer to a question, or a 'please confirm receipt'. NOT requests (they "
    "still get a category): pleasantries and thank-yous; FYIs and generic "
    "statements needing no reply; acknowledgments ('got it', 'received, "
    "thanks'); investor commentary or opinions with no ask; automated "
    "platform notifications (Carta/DocuSign status mail); newsletters and "
    "mass announcements; our own message quoted back with no new ask. When "
    "in doubt about whether a real ask exists, is_request = false — a missed "
    "edge case is cheaper than a junk ticket.\n"
    "Categories (pick the dominant topic as category; if a clearly present "
    "second topic exists, return it as secondary_category, else null):\n"
    "- CapitalCall: capital-call payment traffic — wire confirmations, missed "
    "calls, amount/commitment disputes, management-fee pushback, payment "
    "routing\n"
    "- TaxDocs: K-1s (availability/resends/missing), W-8/W-9, tax returns, "
    "CPA document collection\n"
    "- Valuation: current valuation, share counts, NAV, price-per-share, "
    "custodian year-end valuations\n"
    "- Reporting: fund-level reports, quarterly reports, audit-support info\n"
    "- CapitalAccount: capital account statements — requests, resends, "
    "discrepancies\n"
    "- SubscriptionDocs: subscription/onboarding support — signature failures, "
    "subscription document access/versions, account opening forms\n"
    "- CartaOnboarding: getting onto or into the Carta platform itself — Carta "
    "invitations, account activation/registration, login/access problems, "
    "completing Carta onboarding steps (the platform, not the sub-doc content)\n"
    "- BrokerageDetails: brokerage/DTC account coordinates for delivering shares "
    "— providing or requesting a brokerage account number, DTC participant "
    "number, or delivery instructions (the account details themselves, distinct "
    "from the LiquidityTransfer election/decision to move shares)\n"
    "- KYC-AML: identity documents, FATCA/CRS, compliance verification\n"
    "- AccountAdmin: portal access/passwords, distribution-list changes, "
    "address/email changes, advisor access grants, ownership transfers "
    "between own entities\n"
    "- LiquidityTransfer: sell/hold elections, redemptions, distribution "
    "tracing, secondary interest, share transfers to brokerage (DTC)\n"
    "- NDA: non-disclosure / confidentiality agreements in onboarding or due "
    "diligence — sending, signing, redlining, countersigning, or chasing an "
    "NDA (including mutual NDAs and NDA+non-circumvent bundles)\n"
    "- Legal-SideLetter: side letters, LPA amendments, consent solicitations, "
    "and other legal terms that are NOT an NDA\n"
    "- Meeting (scheduling), DataRoom, Other: as named\n"
    "Also extract:\n"
    "- routing_category — exactly one of four; this drives WHO handles it:\n"
    "  * process_blocker: Carta/KYC/subdoc/tax-form/wire mechanics preventing "
    "completion\n"
    "  * conviction: substantive deal-thesis questions (moat, valuation, "
    "comparisons, risks)\n"
    "  * deal_mechanics: round size, timeline, structure, jurisdiction, "
    "allocation\n"
    "  * scheduling: calls, intros, meeting logistics\n"
    "- description: ONE sentence stating what they want (concrete, no filler)\n"
    "- third_party: true when the sender acts on behalf of an investor (CPA, "
    "wealth advisor, family office, IRA custodian, auditor, bank) rather than "
    "being the investor\n"
    "- confidence: 0-100, how certain you are of the category + is_request\n"
    "- urgency (STATED urgency only — never inferred): 'explicit_deadline' if "
    "a date/time by which they need it is stated (also return the date as "
    "ISO YYYY-MM-DD), 'urgent_language' if words like urgent/ASAP/immediately "
    "appear, else 'none'."
)

REQUEST_SCHEMA = {
    "type": "object",
    "properties": {
        "is_request": {"type": "boolean"},
        "description": {"type": "string"},
        "category": {"type": "string", "enum": REQUEST_CATEGORIES},
        "routing_category": {"type": "string", "enum": ROUTING_CATEGORIES},
        "secondary_category": {"anyOf": [
            {"type": "string", "enum": REQUEST_CATEGORIES},
            {"type": "null"},
        ]},
        "third_party": {"type": "boolean"},
        "confidence": {"type": "integer"},
        "urgency": {"type": "string",
                    "enum": ["none", "urgent_language", "explicit_deadline"]},
        "deadline": {"type": ["string", "null"],
                     "description": "ISO date if explicit_deadline else null"},
    },
    "required": ["is_request", "description", "category", "routing_category",
                 "secondary_category", "third_party", "confidence", "urgency",
                 "deadline"],
    "additionalProperties": False,
}


def classify_request(subject: str, body: str, log=print) -> dict | None:
    """Single-message request detection (WS6). Cached by subject+body hash so a
    re-synced message never re-invokes. Returns the schema dict, or None when
    no key / refused (callers treat as not-a-request)."""
    if not available():
        return None
    cache = _load_cache()
    # req3: v2 taxonomy + routing_category (close-readiness §2.2) — the
    # prefix bump invalidates older-shaped cached verdicts without a purge
    ck = "req3:" + cache_key(subject, body[:500])
    if ck in cache:
        return cache[ck]
    import anthropic
    client = anthropic.Anthropic(api_key=_api_key(), max_retries=4)
    try:
        resp = client.messages.create(
            model=REQUEST_MODEL,
            # max_tokens caps thinking + output together on claude-opus-5 —
            # 512 truncated v2-schema responses mid-JSON (found live 2026-08-05)
            max_tokens=2048,
            system=[{"type": "text", "text": REQUEST_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user",
                       "content": f"Subject: {subject}\n\n{body[:4000]}"}],
            output_config={"format": {"type": "json_schema",
                                      "schema": REQUEST_SCHEMA}},
        )
    except anthropic.APIStatusError as e:
        log(f"[llm] request classify failed ({type(e).__name__}) — treated as "
            "not-a-request; retried on next sync of this thread")
        return None
    if resp.stop_reason == "refusal":
        return None
    try:
        data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    except (json.JSONDecodeError, StopIteration):
        log(f"[llm] request classify response truncated "
            f"(stop_reason={resp.stop_reason}) — skipped, not cached")
        return None
    cache[ck] = data
    _save_cache(cache)
    return data


PROMO_SYSTEM = (
    "You process inbound investor emails already flagged as information "
    "requests for a private-equity IR team. For each, extract:\n"
    "- title: ONE sentence stating what the sender wants (concrete, no filler)\n"
    "- routing_category — exactly one of four; this drives who handles it:\n"
    "  * process_blocker: Carta/KYC/subdoc/tax-form/wire mechanics preventing "
    "completion (login issues, EIN/W-9 problems, doc re-sends)\n"
    "  * conviction: substantive questions about the deal thesis — moat, "
    "valuation, competitive comparisons, risks, track record\n"
    "  * deal_mechanics: round size, timeline, structure, jurisdiction, "
    "allocation, minimums\n"
    "  * scheduling: calls, intros, meeting logistics\n"
    "- category: finer label, one of Reporting, CapitalAccount, Valuation, "
    "KYC-AML, SubscriptionDocs, Legal-SideLetter, Meeting, DataRoom, Other\n"
    "- urgency (STATED only, never inferred): 'explicit_deadline' if a "
    "date/time they need it by is stated (return it as ISO YYYY-MM-DD in "
    "deadline), 'urgent_language' for urgent/ASAP/immediately, else 'none'."
)

PROMO_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "routing_category": {"type": "string", "enum": ROUTING_CATEGORIES},
        "category": {"type": "string", "enum": [
            "Reporting", "CapitalAccount", "Valuation", "KYC-AML",
            "SubscriptionDocs", "Legal-SideLetter", "Meeting", "DataRoom",
            "Other"]},
        "urgency": {"type": "string",
                    "enum": ["none", "urgent_language", "explicit_deadline"]},
        "deadline": {"type": ["string", "null"]},
    },
    "required": ["title", "routing_category", "category", "urgency", "deadline"],
    "additionalProperties": False,
}

LOST_SYSTEM = (
    "You read the LAST inbound email from an investor prospect to a "
    "private-equity IR team and judge whether it is an explicit decline of "
    "the investment (e.g. 'we don't invest at valuations of this level', "
    "'we're going to pass', 'not a fit for us'). Polite deferrals that leave "
    "the door open ('not right now, keep us posted') are NOT declines. "
    "Return declined plus a short verbatim-anchored reason."
)

LOST_SCHEMA = {
    "type": "object",
    "properties": {
        "declined": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["declined", "reason"],
    "additionalProperties": False,
}


def _single_call(system: str, schema: dict, prompt: str, cache_prefix: str,
                 log=print) -> dict | None:
    """One cached structured-output call on REQUEST_MODEL (shared plumbing for
    promotion + closed-lost). Cache key = prefix + sha(prompt head)."""
    if not available():
        return None
    cache = _load_cache()
    ck = cache_prefix + cache_key(system[:40], prompt[:500])
    if ck in cache:
        return cache[ck]
    import anthropic
    client = anthropic.Anthropic(api_key=_api_key(), max_retries=4)
    try:
        resp = client.messages.create(
            model=REQUEST_MODEL,
            max_tokens=1024,
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt[:4000]}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.APIStatusError as e:
        log(f"[llm] {cache_prefix} call failed ({type(e).__name__}) — skipped")
        return None
    if resp.stop_reason == "refusal":
        return None
    try:
        data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    except (json.JSONDecodeError, StopIteration):
        log(f"[llm] {cache_prefix} response truncated "
            f"(stop_reason={resp.stop_reason}) — skipped, not cached")
        return None
    cache[ck] = data
    _save_cache(cache)
    return data


def classify_promotion(subject: str, snippet: str, log=print) -> dict | None:
    """§0.2/§2.2: title + 4-way routing + fine category + urgency for a
    request-flagged signal being promoted to an Information Request."""
    return _single_call(PROMO_SYSTEM, PROMO_SCHEMA,
                        f"Subject: {subject}\n\n{snippet}", "promo:", log)


def classify_closed_lost(subject: str, snippet: str, log=print) -> dict | None:
    """§6: explicit-decline detection on a keyword-prescreened last inbound."""
    return _single_call(LOST_SYSTEM, LOST_SCHEMA,
                        f"Subject: {subject}\n\n{snippet}", "lost:", log)


# §4.4 draft-reply writer. Guardrailed: facts only from context, no commitments,
# no fabricated specifics, body only (the mailer adds the signature). Never sent
# from here — the draft is stored for a human to edit/approve/send.
DRAFT_SYSTEM = (
    "You draft a reply for a private-equity investor-relations team to send to an "
    "investor. Write a professional, warm, concise reply body that addresses the "
    "investor's request. STRICT RULES:\n"
    "- Use ONLY facts present in the provided request and prior correspondence. Do "
    "NOT invent specifics (dates, amounts, document names, timelines).\n"
    "- Only reference something as 'previously shared/sent' if the prior "
    "correspondence clearly shows it was. Never imply the investor was told "
    "something they were not.\n"
    "- Make no commitments, guarantees of timing, or legal/tax statements.\n"
    "- When reference replies from the team are provided, MATCH their tone and "
    "structure (the house voice) and reuse accurate, general content patterns. But "
    "treat them as STYLE ONLY — NEVER copy another investor's specific names, "
    "figures, dates, holdings, or commitments into this reply.\n"
    "- If you lack the information to answer, write a brief holding reply that "
    "acknowledges the request and says a team member will follow up with specifics.\n"
    "- Output the reply BODY only — no subject line, no signature block, no "
    "placeholders in brackets. Keep it under ~180 words."
)


def draft_reply(subject: str, summary: str, prior: list[str],
                exemplars: list[str] | None = None, log=print) -> str | None:
    """§4.4 (T3/T4): draft a reply body from the request + prior correspondence to
    this contact, plus optional cross-investor exemplars (how the team has answered
    similar requests) used for TONE + content patterns only. Plain text; NEVER sent.
    Cached by request + context. None when no API key (caller stores no draft)."""
    if not available():
        return None
    prior_block = "\n\n".join(f"- {(s or '')[:600]}" for s in (prior or [])[:6]) \
        or "(no prior correspondence to this contact on file)"
    ex = [s for s in (exemplars or []) if s][:6]
    ex_block = "\n\n".join(f"- {s[:500]}" for s in ex)
    ex_section = (
        "\nHow the team has answered similar requests — STYLE + CONTENT reference "
        "ONLY (match the tone; do NOT copy any specific names, figures, dates, or "
        f"commitments from these):\n{ex_block}\n" if ex_block else "")
    prompt = (f"Investor request:\nSubject: {subject}\nSummary: {summary}\n\n"
              f"Prior correspondence to this contact (most recent first):\n"
              f"{prior_block}\n{ex_section}\nDraft the reply body only.")
    cache = _load_cache()
    ck = "draft:" + cache_key(subject, (summary + prior_block + ex_block)[:600])
    if ck in cache:
        return cache[ck]
    import anthropic
    client = anthropic.Anthropic(api_key=_api_key(), max_retries=4)
    try:
        resp = client.messages.create(
            model=REQUEST_MODEL, max_tokens=900,
            system=[{"type": "text", "text": DRAFT_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt[:6000]}],
        )
    except anthropic.APIError as e:          # status AND connection errors
        log(f"[llm] draft call failed ({type(e).__name__}) — skipped")
        return None
    if resp.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        return None
    cache[ck] = text
    _save_cache(cache)
    return text


def cache_key(sender: str, subject: str) -> str:
    return hashlib.sha256(f"{sender.lower()}|{subject.lower()}".encode()).hexdigest()[:32]


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}


def _save_cache(cache: dict):
    STATE.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache))


def _api_key() -> str | None:
    k = os.environ.get("ANTHROPIC_API_KEY")
    if k:
        return k
    try:                       # fall back to the project .env (not exported)
        from .config import load_env
        return load_env().get("ANTHROPIC_API_KEY")
    except Exception:
        return None


def available() -> bool:
    return bool(_api_key())


def classify_noise(items: list[dict], log=print) -> dict:
    """items: [{key, sender, subject, snippet}] → {key: label}.
    Cache-first; only cache misses hit the API, in batches. Returns {} if no
    API key (callers then leave items in needs_review)."""
    cache = _load_cache()
    out, misses = {}, []
    for it in items:
        ck = cache_key(it["sender"], it["subject"])
        if ck in cache:
            out[it["key"]] = cache[ck]
        else:
            misses.append((ck, it))
    if not misses:
        return out
    if not available():
        log(f"[llm] no ANTHROPIC_API_KEY — {len(misses)} candidates stay in review")
        return out

    import anthropic
    client = anthropic.Anthropic(api_key=_api_key())
    total_in = total_out = 0
    for i in range(0, len(misses), BATCH_SIZE):
        batch = misses[i:i + BATCH_SIZE]
        lines = [
            f"{j}. from: {it['sender']}\n   subject: {it['subject'][:150]}\n"
            f"   snippet: {(it.get('snippet') or '')[:200]}"
            for j, (_, it) in enumerate(batch)
        ]
        resp = client.messages.create(
            model=NOISE_MODEL,
            max_tokens=2048,
            system=NOISE_SYSTEM,
            messages=[{"role": "user",
                       "content": "Classify each item:\n\n" + "\n".join(lines)}],
            output_config={"format": {"type": "json_schema", "schema": NOISE_SCHEMA}},
        )
        if resp.stop_reason == "refusal":
            log("[llm] batch refused — leaving batch in review")
            continue
        total_in += resp.usage.input_tokens
        total_out += resp.usage.output_tokens
        data = json.loads(next(b.text for b in resp.content if b.type == "text"))
        for v in data["verdicts"]:
            if 0 <= v["index"] < len(batch):
                ck, it = batch[v["index"]]
                cache[ck] = v["label"]
                out[it["key"]] = v["label"]
    _save_cache(cache)
    log(f"[llm] noise triage: {len(misses)} classified in "
        f"{-(-len(misses) // BATCH_SIZE)} call(s), "
        f"{total_in} in / {total_out} out tokens ({NOISE_MODEL})")
    return out
