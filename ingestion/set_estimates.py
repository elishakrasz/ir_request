"""Bulk-set Dynamics Estimated Revenue from the filled-in gap worklist
(finding 2: soft/verbal commitment tiers are $0 because estimatedvalue is
unpopulated on live opps).

Input: Tokens/data/estimated_revenue_gaps.csv with the last column filled
(plain numbers, e.g. 250000). Rows with an empty amount are skipped.
Dry run default; --apply patches opportunity.estimatedvalue (the Engagement
Ingestion role already carries Opportunity Write).

Usage:
    python -m ingestion.set_estimates            # dry run
    python -m ingestion.set_estimates --apply
"""
import argparse
import csv
import re
from pathlib import Path

from .config import Config
from .dataverse_client import DataverseClient
from .sync import say

CSV_PATH = Path("/home/emallard/Tokens/data/estimated_revenue_gaps.csv")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--csv", default=str(CSV_PATH))
    args = ap.parse_args()
    cfg = Config.from_env()
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, cfg.prefix, apply=args.apply)

    opps = dv.query("opportunities?$select=opportunityid,name,new_prospectcode,"
                    "estimatedvalue&$filter=statecode eq 0 and new_live eq true")
    by_code = {(o.get("new_prospectcode") or "").strip(): o for o in opps}

    plan, skipped = [], 0
    for row in csv.DictReader(open(args.csv)):
        raw = (row.get("estimated_usd_FILL_ME") or "").strip()
        amount = re.sub(r"[^0-9.]", "", raw)
        if not amount:
            skipped += 1
            continue
        opp = by_code.get((row.get("code") or "").strip())
        if not opp:
            say(f"  no live opp for code {row.get('code')!r} — skipped")
            continue
        plan.append((opp["opportunityid"], opp.get("name", ""), float(amount)))

    print(f"{len(plan)} estimate(s) to set, {skipped} rows without an amount")
    for oid, name, amt in plan[:15]:
        print(f"  {name[:55]:55} → ${amt:,.0f}")
    if not args.apply:
        print("\nDRY RUN — fill amounts in the CSV, then --apply.")
        return
    for oid, name, amt in plan:
        dv.patch("opportunities", oid, {"estimatedvalue": amt},
                 f"estimate {name[:40]}")
    say(f"done — {len(plan)} opportunities updated")


if __name__ == "__main__":
    main()
