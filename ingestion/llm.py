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


REQUEST_SYSTEM = (
    "You screen inbound investor emails for a private-equity IR team, "
    "detecting information requests that require a response or action.\n"
    "An information request = the sender asks the firm for something: a "
    "document, a figure, a signature, a meeting, access, a status update, an "
    "answer to a question. Mere pleasantries, FYIs, and confirmations are not "
    "requests.\n"
    "If it IS a request, also extract:\n"
    "- description: ONE sentence stating what they want (concrete, no filler)\n"
    "- category: one of Reporting, CapitalAccount, Valuation, KYC-AML, "
    "SubscriptionDocs, Legal-SideLetter, Meeting, DataRoom, Other\n"
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
        "category": {"type": "string", "enum": [
            "Reporting", "CapitalAccount", "Valuation", "KYC-AML",
            "SubscriptionDocs", "Legal-SideLetter", "Meeting", "DataRoom",
            "Other"]},
        "urgency": {"type": "string",
                    "enum": ["none", "urgent_language", "explicit_deadline"]},
        "deadline": {"type": ["string", "null"],
                     "description": "ISO date if explicit_deadline else null"},
    },
    "required": ["is_request", "description", "category", "urgency", "deadline"],
    "additionalProperties": False,
}


def classify_request(subject: str, body: str, log=print) -> dict | None:
    """Single-message request detection (WS6). Cached by subject+body hash so a
    re-synced message never re-invokes. Returns the schema dict, or None when
    no key / refused (callers treat as not-a-request)."""
    if not available():
        return None
    cache = _load_cache()
    ck = "req:" + cache_key(subject, body[:500])
    if ck in cache:
        return cache[ck]
    import anthropic
    client = anthropic.Anthropic(api_key=_api_key(), max_retries=4)
    try:
        resp = client.messages.create(
            model=REQUEST_MODEL,
            max_tokens=512,
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
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    cache[ck] = data
    _save_cache(cache)
    return data


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
