"""2-year ir@ inbox categorization report.

Pulls every ir@exigentcap.com inbox message since --since (default 2y), and
classifies each with ONE batched cheap-model (Haiku) call per 40 into:
  - type: investor_correspondence | newsletter_marketing |
          automated_notification | internal_ops
  - category (when investor_correspondence): the IR request taxonomy
  - is_request: does the sender actually ask the firm for something

Then writes reports/ir_history_<ts>.xlsx (+ Windows Downloads copy):
  Summary (type + category + is-request splits, quarterly trend),
  By Month, By Category, Top Senders, All Emails (classified rows).

Cost control: verdicts cached in state/ir_history_cache.json (sha of
sender+subject), so re-runs are near-free. Read-only against Graph/LLM —
no Dataverse writes.

Usage:  venv/bin/python -m ingestion.report_ir_history [--since 2024-08-06] [--apply-llm]
        (without --apply-llm it classifies from cache only / heuristics — a dry sizing)
"""
import argparse
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

from .config import Config, STATE_DIR
from .graph_client import GraphClient
from . import llm

MB = "ir@exigentcap.com"
GRAPH = "https://graph.microsoft.com/v1.0"
CACHE = STATE_DIR / "ir_history_cache.json"
WIN_DOWNLOADS = Path("/mnt/c/Users/emallard/Downloads")

TYPE_LABELS = ["investor_correspondence", "newsletter_marketing",
               "automated_notification", "internal_ops"]
TYPE_PRETTY = {
    "investor_correspondence": "Investor correspondence",
    "newsletter_marketing": "Newsletter / marketing",
    "automated_notification": "Automated notification",
    "internal_ops": "Internal ops",
}
CATEGORIES = ["Reporting", "CapitalAccount", "CapitalCall", "TaxDocs",
              "Valuation", "KYC-AML", "SubscriptionDocs", "AccountAdmin",
              "LiquidityTransfer", "Legal-SideLetter", "Meeting", "DataRoom",
              "Other"]

SYSTEM = (
    "You categorize inbound emails to a private-equity investor-relations "
    "mailbox for a historical analysis. For each item classify:\n"
    "- type: 'investor_correspondence' (a real human message from/for an "
    "investor or their advisor — questions, documents, payments, meetings), "
    "'newsletter_marketing' (news digests, market commentary, event/product "
    "promotion, mass mail), 'automated_notification' (platform/robot mail: "
    "Carta/DocuSign/bank status, receipts, calendar, delivery reports), or "
    "'internal_ops' (internal firm IT/HR/payroll/admin).\n"
    "- category: for investor_correspondence pick the dominant topic — "
    "Reporting, CapitalAccount, CapitalCall, TaxDocs (K-1s/W-8/W-9), "
    "Valuation, KYC-AML, SubscriptionDocs, AccountAdmin (portal/access/"
    "address/distribution-list), LiquidityTransfer (sell/redeem/DTC/transfer), "
    "Legal-SideLetter, Meeting, DataRoom, Other; else null.\n"
    "- is_request: true if the sender asks the firm FOR something (document, "
    "figure, signature, meeting, access, status, an answer); false for "
    "FYIs/acknowledgements/statements/automated mail."
)
SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "type": {"type": "string", "enum": TYPE_LABELS},
            "category": {"anyOf": [{"type": "string", "enum": CATEGORIES}, {"type": "null"}]},
            "is_request": {"type": "boolean"},
        },
        "required": ["index", "type", "category", "is_request"],
        "additionalProperties": False,
    }}}, "required": ["verdicts"], "additionalProperties": False,
}


def key(sender, subject):
    return hashlib.sha256(f"{sender.lower()}|{subject.lower()}".encode()).hexdigest()[:32]


def pull(g, since):
    sel = "id,subject,bodyPreview,from,receivedDateTime,webLink"
    url = (f"{GRAPH}/users/{MB}/mailFolders/inbox/messages?$select={sel}"
           f"&$top=999&$filter=receivedDateTime ge {since}T00:00:00Z"
           f"&$orderby=receivedDateTime desc")
    out = []
    while url:
        j = g._get(url).json()
        out.extend(j.get("value", []))
        url = j.get("@odata.nextLink")
    rows = []
    for m in out:
        rows.append({
            "sender": ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower(),
            "subject": m.get("subject") or "",
            "snippet": m.get("bodyPreview") or "",
            "ts": m.get("receivedDateTime") or "",
        })
    return rows


def classify(rows, apply_llm):
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    todo = []
    for r in rows:
        ck = key(r["sender"], r["subject"])
        if ck in cache:
            r.update(cache[ck])
        else:
            todo.append((ck, r))
    print(f"  {len(rows) - len(todo)} cached, {len(todo)} to classify")
    if todo and apply_llm and llm.available():
        import anthropic
        client = anthropic.Anthropic(api_key=llm._api_key())
        B = 40
        for i in range(0, len(todo), B):
            batch = todo[i:i + B]
            lines = [f"{j}. from: {r['sender']}\n   subject: {r['subject'][:150]}\n"
                     f"   preview: {(r['snippet'] or '')[:200]}"
                     for j, (_, r) in enumerate(batch)]
            resp = client.messages.create(
                model=llm.NOISE_MODEL, max_tokens=4096, system=SYSTEM,
                messages=[{"role": "user", "content": "Classify each:\n\n" + "\n".join(lines)}],
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
            if resp.stop_reason == "refusal":
                continue
            data = json.loads(next(b.text for b in resp.content if b.type == "text"))
            for v in data["verdicts"]:
                if 0 <= v["index"] < len(batch):
                    ck, r = batch[v["index"]]
                    verdict = {"type": v["type"], "category": v.get("category"),
                               "is_request": bool(v.get("is_request"))}
                    cache[ck] = verdict
                    r.update(verdict)
            print(f"    classified {min(i + B, len(todo))}/{len(todo)}", flush=True)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache))
    # anything still unclassified (no LLM / refused) → unknown bucket
    for r in rows:
        r.setdefault("type", "unclassified")
        r.setdefault("category", None)
        r.setdefault("is_request", False)
    return rows


def domain(s):
    return s.split("@")[-1] if "@" in s else "(none)"


def write_xlsx(rows, since):
    HEAD = PatternFill("solid", start_color="1F4E79")
    HF = Font(bold=True, color="FFFFFF", size=10)
    DF = Font(size=10)
    wb = Workbook(); wb.remove(wb.active)

    def sheet(name, headers, data, widths):
        ws = wb.create_sheet(name)
        for i, h in enumerate(headers, 1):
            c = ws.cell(1, i, h); c.font = HF; c.fill = HEAD
            c.alignment = Alignment(horizontal="center")
        for ri, row in enumerate(data, 2):
            for ci, v in enumerate(row, 1):
                ws.cell(ri, ci, v).font = DF
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[chr(64 + i)].width = w
        ws.freeze_panes = "A2"
        return ws

    n = len(rows)
    by_type = Counter(r["type"] for r in rows)
    inv = [r for r in rows if r["type"] == "investor_correspondence"]
    by_cat = Counter(r["category"] or "(uncategorized)" for r in inv)
    reqs = sum(1 for r in inv if r["is_request"])

    summ = [("Total emails", n),
            ("Date range", f"{min(r['ts'][:10] for r in rows)} → {max(r['ts'][:10] for r in rows)}"),
            ("", "")]
    summ += [(f"Type · {TYPE_PRETTY.get(t, t)}", f"{c}  ({c*100//n}%)")
             for t, c in by_type.most_common()]
    summ += [("", ""), ("Investor correspondence", len(inv)),
             ("  … actual requests (asks)", f"{reqs}  ({reqs*100//max(len(inv),1)}%)"),
             ("  … FYIs / statements", len(inv) - reqs), ("", "")]
    summ += [(f"Category · {cat}", c) for cat, c in by_cat.most_common()]
    sheet("Summary", ["Metric", "Value"], summ, [40, 40])

    months = sorted({r["ts"][:7] for r in rows if r["ts"]})
    mtypes = defaultdict(Counter)
    for r in rows:
        if r["ts"]:
            mtypes[r["ts"][:7]][r["type"]] += 1
    sheet("By Month", ["Month", "Total", *[TYPE_PRETTY[t] for t in TYPE_LABELS]],
          [[m, sum(mtypes[m].values()), *[mtypes[m].get(t, 0) for t in TYPE_LABELS]]
           for m in months], [12, 10, 24, 22, 22, 14])

    catrows = []
    for cat, c in by_cat.most_common():
        cr = [r for r in inv if (r["category"] or "(uncategorized)") == cat]
        catrows.append([cat, c, sum(1 for r in cr if r["is_request"])])
    sheet("By Category", ["Category (investor mail)", "Emails", "Actual requests"],
          catrows, [28, 12, 16])

    dom = Counter(domain(r["sender"]) for r in rows)
    sheet("Top Senders", ["Sender domain", "Emails"],
          dom.most_common(40), [40, 12])

    allrows = sorted(rows, key=lambda r: r["ts"], reverse=True)
    sheet("All Emails", ["Received", "Sender", "Subject", "Type", "Category", "Request?"],
          [[r["ts"][:16].replace("T", " "), r["sender"], r["subject"][:90],
            TYPE_PRETTY.get(r["type"], r["type"]), r["category"] or "",
            "yes" if r["is_request"] else ""] for r in allrows],
          [17, 34, 60, 22, 18, 10])

    out_dir = Path(__file__).resolve().parent.parent / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"ir_history_{datetime.now(timezone.utc):%Y%m%d_%H%M}.xlsx"
    wb.save(out)
    print(f"wrote {out}")
    if WIN_DOWNLOADS.is_dir():
        shutil.copy2(out, WIN_DOWNLOADS / out.name)
        print(f"copied to {WIN_DOWNLOADS / out.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2024-08-06")
    ap.add_argument("--apply-llm", action="store_true")
    args = ap.parse_args()
    cfg = Config.from_env()
    g = GraphClient(cfg.tenant_id, cfg.client_id, cfg.client_secret)
    print(f"pulling ir@ inbox since {args.since} …")
    rows = pull(g, args.since)
    print(f"  {len(rows)} messages")
    print("classifying …")
    rows = classify(rows, args.apply_llm)
    write_xlsx(rows, args.since)


if __name__ == "__main__":
    main()
