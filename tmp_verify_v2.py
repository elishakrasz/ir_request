"""READ-ONLY classifier-v2 verification over live PROD traffic (last 14 days).

Pulls meaningful, non-excluded Inbound signals, runs the PRODUCTION classify()
path (llm.classify_request, req2: cache) on subject+snippet, and reports v2
verdicts vs. the v1 flags stored at ingestion. Zero Dataverse writes.

Output: reports/v2_verify.tsv + stdout summary.
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ingestion.classify import classify
from ingestion.config import Config
from ingestion.dataverse_client import DataverseClient
from ingestion.sync import say

ROOT = Path(__file__).parent
OUT = ROOT / "reports" / "v2_verify.tsv"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--limit", type=int, default=0, help="cap rows (0 = all)")
    args = ap.parse_args()

    cfg = Config.from_env()
    ch, p = cfg.choices, cfg.prefix
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=False)

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    say(f"pulling Inbound signals since {since}…")
    rows = dv.query(
        f"{p}engagementsignals?$select={p}engagementsignalid,{p}name,{p}sender,"
        f"{p}snippet,{p}timestamputc,{p}isinforequest,{p}rfistatus,"
        f"{p}messagekeyhash,{p}provenance"
        f"&$filter={p}direction eq {ch.direction['Inbound']} "
        f"and {p}ismeaningful eq true "
        f"and {p}matchstatus ne {ch.matchstatus['Excluded']} "
        f"and {p}timestamputc ge {since}")

    # one row per message (N contacts share the hash) — classifier input is
    # identical, so dedupe up front; the req2: cache would dedupe cost anyway
    seen, msgs = set(), []
    for r in sorted(rows, key=lambda r: r.get(f"{p}timestamputc") or ""):
        h = r.get(f"{p}messagekeyhash")
        if h in seen:
            continue
        seen.add(h)
        msgs.append(r)
    if args.limit:
        msgs = msgs[:args.limit]
    say(f"{len(rows)} rows → {len(msgs)} distinct messages; classifying (v2)…")

    results = []
    for i, r in enumerate(msgs, 1):
        subject = r.get(f"{p}name") or ""
        snippet = r.get(f"{p}snippet") or ""
        rfi = classify(subject, snippet[:4000])   # PRODUCTION path, req2: cache
        results.append((r, rfi))
        if i % 20 == 0:
            say(f"  {i}/{len(msgs)}")

    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        f.write("timestamp\tsender\tsubject\tv1_isreq\tv2_isreq\tprimary\t"
                "secondary\tthird_party\tconfidence\tdescription\n")
        for r, rfi in results:
            f.write("\t".join([
                (r.get(f"{p}timestamputc") or "")[:16],
                r.get(f"{p}sender") or "",
                (r.get(f"{p}name") or "")[:80],
                "1" if r.get(f"{p}isinforequest") else "0",
                "1" if rfi.is_info_request else "0",
                rfi.category or "",
                rfi.secondary or "",
                "1" if rfi.third_party else "0",
                str(rfi.confidence),
                rfi.description[:120],
            ]) + "\n")

    v1_req = sum(1 for r, _ in results if r.get(f"{p}isinforequest"))
    v2_req = sum(1 for _, rfi in results if rfi.is_info_request)
    agree = sum(1 for r, rfi in results
                if bool(r.get(f"{p}isinforequest")) == rfi.is_info_request)
    cats = Counter(rfi.category for _, rfi in results if rfi.is_info_request)
    tp = sum(1 for _, rfi in results if rfi.third_party)
    lo = sum(1 for _, rfi in results if rfi.is_info_request and rfi.confidence < 70)
    print(f"\nmessages: {len(results)}")
    print(f"v1 flagged requests: {v1_req}   v2 flagged: {v2_req}   "
          f"agreement: {agree}/{len(results)}")
    print(f"third_party: {tp}   v2-requests with confidence<70: {lo}")
    for k, v in cats.most_common():
        print(f"  {v:3d}  {k}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
