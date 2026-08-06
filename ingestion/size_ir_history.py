"""Read-only sizing for a 2-year ir@ inbox categorization report.

Pulls ir@exigentcap.com inbox message metadata since --since (default 2y),
runs the WS1 noise-gate heuristics to split noise vs LLM-candidates, and
prints volume + a monthly histogram. No LLM calls, no writes — this just
sizes the classification job before we spend on it.

Usage:  venv/bin/python -m ingestion.size_ir_history [--since 2024-08-06]
"""
import argparse
from collections import Counter

from .config import Config
from .graph_client import GraphClient
from . import matching, noise

MB = "ir@exigentcap.com"
GRAPH = "https://graph.microsoft.com/v1.0"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2024-08-06")
    args = ap.parse_args()
    cfg = Config.from_env()
    g = GraphClient(cfg.tenant_id, cfg.client_id, cfg.client_secret)

    sel = "id,subject,bodyPreview,from,receivedDateTime"
    url = (f"{GRAPH}/users/{MB}/mailFolders/inbox/messages"
           f"?$select={sel}&$top=999"
           f"&$filter=receivedDateTime ge {args.since}T00:00:00Z"
           f"&$orderby=receivedDateTime desc")

    msgs, pages = [], 0
    while url:
        r = g._get(url)
        j = r.json()
        msgs.extend(j.get("value", []))
        url = j.get("@odata.nextLink")
        pages += 1
        if pages % 5 == 0:
            print(f"  … {len(msgs)} messages so far", flush=True)

    print(f"\nir@ inbox since {args.since}: {len(msgs)} messages ({pages} pages)")
    if not msgs:
        return
    dates = [m.get("receivedDateTime", "")[:10] for m in msgs if m.get("receivedDateTime")]
    print(f"date range: {min(dates)} → {max(dates)}")

    verdicts = Counter()
    for m in msgs:
        sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
        subj = m.get("subject") or ""
        body = m.get("bodyPreview") or ""
        internal = matching.domain_of(sender) in cfg.org_domains
        v, _reason = noise.gate(sender, subj, body, cfg.rules,
                                sender_is_matched_contact=False,
                                sender_is_internal=internal)
        verdicts[v] += 1
    print("\nnoise-gate split (heuristics only, pre-LLM):")
    for v, n in verdicts.most_common():
        print(f"  {v:12s} {n}")
    llm_load = verdicts.get("candidate", 0) + verdicts.get("keep", 0)
    print(f"\n→ ~{llm_load} messages would hit the LLM "
          f"(candidates + kept); {verdicts.get('noise', 0)} skipped by heuristics")

    by_month = Counter(m.get("receivedDateTime", "")[:7] for m in msgs
                       if m.get("receivedDateTime"))
    print("\nmonthly volume:")
    for ym in sorted(by_month):
        print(f"  {ym}  {by_month[ym]:4d}  {'█' * (by_month[ym] // 5)}")


if __name__ == "__main__":
    main()
