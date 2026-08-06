"""Backfill new_category on existing engagement signals from the ir@ route.

Going forward, run_ir_request.py tags each new signal with its LLM category
(TAG_SIGNAL_CATEGORY). This one-off backfills the signals that predate the
v3 solution import: it classifies each from its stored subject + snippet
(no Graph re-fetch) and PATCHes new_category.

Scope: inbound signals whose provenance mailbox is one of REQUEST_MAILBOXES
(ir@/mravid@/lgruber@) and that have no category yet. Dry-run by default;
pass --apply to write. No-ops (with a clear message) until the new_category
column exists in the target env.

    python -m ingestion.backfill_signal_category            # dry run
    python -m ingestion.backfill_signal_category --apply
"""
from __future__ import annotations

import argparse
import os

from .classify import classify
from .config import Config, load_env
from .dataverse_client import DataverseClient

DEFAULT_REQ = "ir@exigentcap.com,mravid@exigentcap.com,lgruber@exigentcap.com"


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill signal categories (ir@ route).")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--limit", type=int, default=0, help="cap rows (0 = all)")
    args = ap.parse_args()

    env = load_env()
    cfg = Config.from_env(env)
    p, ch = cfg.prefix, cfg.choices
    req_mb = {m.strip().lower() for m in
              env.get("REQUEST_MAILBOXES", DEFAULT_REQ).split(",") if m.strip()}
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=args.apply)

    if not dv.has_attribute(f"{p}engagementsignal", f"{p}category"):
        raise SystemExit(
            f"new_category not on {p}engagementsignal yet — import the v3 solution "
            "(solution/provision.py) to PROD first, then re-run.")

    # inbound, meaningful, has a category-less category slot; carry provenance +
    # subject + snippet so we can classify without a Graph round-trip.
    inbound = ch.direction["Inbound"]
    rows = dv.query(
        f"{p}engagementsignals?$select={p}engagementsignalid,{p}name,{p}snippet,"
        f"{p}provenance,{p}category,{p}direction,{p}ismeaningful"
        f"&$filter={p}direction eq {inbound} and {p}category eq null")
    targets = []
    for r in rows:
        prov = (r.get(f"{p}provenance") or "")
        mb = prov.split("|", 1)[0].strip().lower()
        if mb in req_mb and r.get(f"{p}ismeaningful"):
            targets.append(r)
    if args.limit:
        targets = targets[:args.limit]

    print(f"{'APPLY' if args.apply else 'DRY-RUN'} — {len(targets)} ir@-route "
          f"signal(s) missing a category (of {len(rows)} uncategorized inbound)")
    tagged = skipped = 0
    for r in targets:
        subj = r.get(f"{p}name") or ""
        snip = r.get(f"{p}snippet") or ""
        rfi = classify(subj, f"{subj}\n\n{snip}")
        if not rfi.category:
            skipped += 1
            continue
        val = _category_value(dv, cfg, rfi.category)
        rid = r[f"{p}engagementsignalid"]
        label = f"{rfi.category:<16} {subj[:56]}"
        if args.apply:
            dv.patch(f"{p}engagementsignals", rid, {f"{p}category": val},
                     f"signal category {rid[:8]}…")
        else:
            print(f"  would tag {label}")
        tagged += 1
    print(f"\n{'tagged' if args.apply else 'would tag'}: {tagged}  "
          f"(no category from classifier: {skipped})")


def _category_value(dv: DataverseClient, cfg: Config, cat: str) -> int:
    """Mirror sync._category_value fold rules for a standalone backfill."""
    p, ch = cfg.prefix, cfg.choices
    v2_only = {"CapitalCall", "TaxDocs", "AccountAdmin", "LiquidityTransfer"}
    cat = cat or "Other"
    if cat == "NDA" and not dv.has_attribute(f"{p}engagementsignal", f"{p}category"):
        cat = "Legal-SideLetter"
    if cat in v2_only and not dv.has_attribute(f"{p}inforequest", f"{p}thirdparty"):
        cat = "Other"
    return ch.req_category.get(cat, ch.req_category["Other"])


if __name__ == "__main__":
    main()
