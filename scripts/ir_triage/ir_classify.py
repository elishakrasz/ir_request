"""Prototype triage classifier over the deduped human threads (READ-ONLY, local).

Runs claude-opus-5 with structured outputs over reports/ir_human.tsv in batches,
assigning primary/secondary category, is_request, third_party, confidence.
Output: reports/ir_classified.tsv + summary. Nothing touches Dataverse/Graph.
"""
import json
import re
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parents[2]
HUMAN = ROOT / "reports" / "ir_human.tsv"
OUT = ROOT / "reports" / "ir_classified.tsv"
CKPT = ROOT / "reports" / "ir_classified_partial.jsonl"
BATCH = 25

CATEGORIES = ["CapitalCall", "TaxDocs", "Valuation", "Reporting",
              "SubscriptionDocs", "AccountAdmin", "LiquidityTransfer",
              "FundStatus", "Meeting", "Verification", "Noise", "Other"]

SYSTEM = """You classify inbound emails to ir@exigentcap.com, the investor-relations \
mailbox of Exigent Capital, a private equity firm. Senders are LPs (investors), their \
CPAs/wealth advisors/family offices, IRA custodians (Inspira, Vantage, Equity Trust, \
Pacific Premier, Midwest Trust), auditors, and fund partners (HighPost, Apex staff). \
Funds include Exigent HP Fund I-A/B, HIPstr, xAI/X Corp/SpaceX holdings, SynthBee, \
Focused Holdings, Total Return, Enhanced Income, Clean Carbon, HighPost co-investments.

Categories:
- CapitalCall: capital-call payment traffic — wire sent/confirmations, missed calls, \
amount or commitment disputes, management-fee pushback, payment routing questions.
- TaxDocs: K-1s (availability, resends, missing federal/state), W-8/W-9, tax returns, \
CPA document collection.
- Valuation: current valuation, share counts, NAV, price-per-share math, carry impact, \
custodian year-end valuation requirements.
- Reporting: capital account statements, financial statements, quarterly reports — \
requests, resends, discrepancies, audit-support info requests.
- SubscriptionDocs: subscription/onboarding support — Carta issues, signature failures, \
KYC/ID docs, account opening forms, subscription document access.
- AccountAdmin: portal access/passwords, add/remove emails on distributions, address or \
email changes, granting advisor access, FATCA/CRS updates, ownership transfers between \
entities (not sales).
- LiquidityTransfer: sell/hold elections, redemptions, distribution tracing, secondary \
buy/sell interest, share transfers to brokerage (DTC instructions, receiving firm info).
- FundStatus: "any update on X?", questions about fund structure/strategy/events, \
replies to investor updates asking substantive questions.
- Meeting: scheduling/rescheduling calls and Zooms.
- Verification: "is this email/DocuSign legitimate?" checks.
- Noise: spam, marketing, vendor sales, system test emails, misdirected mail.
- Other: none of the above fits.

Rules:
- primary = dominant topic; secondary = a clearly present second topic, else null.
- is_request = true only if the email needs an action or reply from the IR team \
(a question, a document to send, an access grant, a "please confirm receipt"). \
Pure thank-yous, FYI notes, and statements needing no reply are is_request=false \
(they still get a category).
- third_party = true when the sender is acting on behalf of an investor (CPA, advisor, \
family office, custodian, auditor, bank) rather than being the investor.
- confidence 0-100. Some previews contain quoted reply chains; judge the NEW content."""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "primary": {"type": "string", "enum": CATEGORIES},
                    "secondary": {"anyOf": [
                        {"type": "string", "enum": CATEGORIES},
                        {"type": "null"},
                    ]},
                    "is_request": {"type": "boolean"},
                    "third_party": {"type": "boolean"},
                    "confidence": {"type": "integer"},
                },
                "required": ["index", "primary", "secondary", "is_request",
                             "third_party", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

IRONSCALES = re.compile(
    r"IRONSCALES (couldn't recognize this email.*?sender \S+ @ [\w.-]+\s*"
    r"|finds this email suspicious!.*?(\| Know this sender\?|attempt\.?\s*\|?)\s*)",
    re.S)
QUOTE = re.compile(
    r"(\bOn .{5,80}wrote:|From: .{3,80}Sent:|-----\s*Original Message|"
    r"Begin forwarded message:|________________________________).*$", re.S)
WS = re.compile(r"\s+")


def clean(text: str) -> str:
    text = IRONSCALES.sub("", text or "")
    text = QUOTE.sub("", text)
    return WS.sub(" ", text).strip()


def load_env_key() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no ANTHROPIC_API_KEY in .env")


def main():
    rows = HUMAN.read_text(encoding="utf-8").splitlines()[1:]
    emails = []
    for i, r in enumerate(rows):
        parts = r.split("\t")
        received, frm, subject, preview = (parts + ["", "", "", ""])[:4]
        emails.append({"index": i, "received": received, "from": frm,
                       "subject": clean(subject), "preview": clean(preview)})

    client = anthropic.Anthropic(api_key=load_env_key())
    results = {}
    if CKPT.exists():  # resume from checkpoint
        for line in CKPT.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            results[item["index"]] = item
        print(f"resuming: {len(results)} already classified", flush=True)

    todo = [e for e in emails if e["index"] not in results]
    try:
        for start in range(0, len(todo), BATCH):
            chunk = todo[start:start + BATCH]
            payload = [{k: e[k] for k in ("index", "from", "subject", "preview")}
                       for e in chunk]
            resp = client.messages.create(
                model="claude-opus-5",
                max_tokens=16000,
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": SCHEMA},
                },
                system=SYSTEM,
                messages=[{"role": "user", "content":
                           "Classify each email:\n" + json.dumps(payload, ensure_ascii=False)}],
            )
            if resp.stop_reason == "refusal":
                print(f"  batch at {start}: refusal — skipped", flush=True)
                continue
            data = json.loads(next(b.text for b in resp.content if b.type == "text"))
            with CKPT.open("a", encoding="utf-8") as f:
                for item in data["items"]:
                    results[item["index"]] = item
                    f.write(json.dumps(item) + "\n")
            print(f"  classified {len(results)}/{len(emails)}", flush=True)
    except anthropic.APIStatusError as e:
        print(f"\nAPI error ({e.status_code}): {e.message}\n"
              f"checkpoint kept at {CKPT} — re-run to resume.", flush=True)

    with OUT.open("w", encoding="utf-8") as f:
        f.write("received\tfrom\tsubject\tprimary\tsecondary\tis_request"
                "\tthird_party\tconfidence\tpreview\n")
        for e in emails:
            r = results.get(e["index"])
            if not r:
                continue
            f.write("\t".join([
                e["received"], e["from"], e["subject"][:100],
                r["primary"], r["secondary"] or "",
                "1" if r["is_request"] else "0",
                "1" if r["third_party"] else "0",
                str(r["confidence"]), e["preview"][:140],
            ]) + "\n")

    # summary
    from collections import Counter
    prim = Counter(r["primary"] for r in results.values())
    req = sum(1 for r in results.values() if r["is_request"])
    tp = sum(1 for r in results.values() if r["third_party"])
    lo = sum(1 for r in results.values() if r["confidence"] < 70)
    print(f"\ntotal classified: {len(results)}")
    print(f"is_request: {req}  third_party: {tp}  confidence<70: {lo}")
    for k, v in prim.most_common():
        n_req = sum(1 for r in results.values()
                    if r["primary"] == k and r["is_request"])
        print(f"  {v:4d}  {k:18s} ({n_req} requests)")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
