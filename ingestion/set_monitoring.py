"""Bulk-set Active Monitoring / Monitoring Start / Aliases on open opportunities.

Dry-run by default (house rule 4); DEV-only (house rule 1, via Config guard);
idempotent — rows already carrying the requested values are skipped.

Aliases: if an opportunity has no aliases yet, seed them from its fund's display
name (plus a variant without the trailing legal suffix) so the content-match
rule can disambiguate multi-fund contacts. Existing aliases are never stomped.

Usage:
    python -m ingestion.set_monitoring --all --start 2026-01-01            # dry run
    python -m ingestion.set_monitoring --fund "HighPost" --start 2026-01-01 --apply
"""
import argparse
import re

from .config import Config
from .dataverse_client import DataverseClient

FMT = "@OData.Community.Display.V1.FormattedValue"


def fund_aliases(fund_name: str) -> list[str]:
    out = [fund_name.strip()]
    bare = re.sub(r",?\s*(L\.?P\.?|LLC|Ltd\.?|Inc\.?)\s*$", "", fund_name,
                  flags=re.I).strip().rstrip(",")
    if bare and bare.lower() != out[0].lower():
        out.append(bare)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fund", action="append", default=[],
                    help="fund name substring (repeatable, case-insensitive)")
    ap.add_argument("--all", action="store_true", help="all open opportunities")
    ap.add_argument("--start", required=True, help="monitoring start date YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()
    if not (args.all or args.fund):
        ap.error("pass --all or at least one --fund")

    cfg = Config.from_env()
    p = cfg.prefix
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=args.apply)
    rows = dv.fetch_opportunities(cfg.fund_lookup)
    fund_key = f"_{cfg.fund_lookup}_value"

    by_fund, skipped, todo = {}, 0, []
    for o in rows:
        fname = o.get(fund_key + FMT) or "(no fund)"
        if not args.all and not any(f.lower() in fname.lower() for f in args.fund):
            continue
        by_fund.setdefault(fname, [0, 0])
        payload = {}
        if not o.get(f"{p}activemonitoring"):
            payload[f"{p}activemonitoring"] = True
        if not o.get(f"{p}monitoringstartdate"):
            payload[f"{p}monitoringstartdate"] = args.start
        if not (o.get(f"{p}aliases") or "").strip() and fname != "(no fund)":
            payload[f"{p}aliases"] = "\n".join(fund_aliases(fname))
        if payload:
            todo.append((o["opportunityid"], o.get("name") or "(unnamed)", payload))
            by_fund[fname][0] += 1
        else:
            skipped += 1
            by_fund[fname][1] += 1

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"{mode}: {len(todo)} opportunities to update, {skipped} already set\n")
    for fname, (n_todo, n_skip) in sorted(by_fund.items()):
        print(f"  {fname}: {n_todo} to update, {n_skip} already set")
    if todo:
        print("\nSample:")
        for oid, name, payload in todo[:3]:
            print(f"  {name[:60]} -> {payload}")
    if args.apply:
        for oid, name, payload in todo:
            dv.patch("opportunities", oid, payload, f"opp {name[:40]}")
        print(f"\nPatched {dv.patched} opportunities.")
    else:
        print("\nRe-run with --apply after review.")


if __name__ == "__main__":
    main()
