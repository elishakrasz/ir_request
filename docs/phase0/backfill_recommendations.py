"""Backfill the Phase 1-3 recommendation/draft fields onto EXISTING requests, so
the IR desk shows them without waiting for new mail. Reuses the live sync
recommender (SyncRun) so backfilled values match what new requests get:

  - assigneerecommended + reason + provenance   (routing rule → responder → owner)
  - drafttier                                    (routing rule tier, T1 default)
  - draftreply + provenance   (T3/T4 only)       (LLM from the request + history)

NOT touched: category (the classifier's original stays), ownerid / any *actual, and
casenumber (a system autonumber). Idempotent: a request that already has an assignee
recommendation is skipped unless --force.

    venv/bin/python docs/phase0/backfill_recommendations.py            # dry run
    venv/bin/python docs/phase0/backfill_recommendations.py --apply --limit 8

House rule: writing to PROD requires an explicit operator "commit to PROD".
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from ingestion.config import Config, load_env
from ingestion.dataverse_client import DataverseClient
from ingestion.classify import RfiResult
from ingestion.sync import SyncRun


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--limit", type=int, default=8, help="most-recent N requests")
    ap.add_argument("--all", action="store_true", help="include closed requests too")
    ap.add_argument("--force", action="store_true", help="re-backfill even if set")
    args = ap.parse_args()

    cfg = Config.from_env(load_env())
    p = cfg.prefix
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, cfg.prefix, apply=args.apply)
    run = SyncRun(cfg, graph=None, dv=dv, apply=args.apply)
    val2code = {v: k for k, v in cfg.choices.req_category.items()}
    print(f"{'APPLY (writing)' if args.apply else 'DRY RUN'} → {cfg.dataverse_url}  "
          f"(most-recent {args.limit})")

    # Default: the OPEN desk (what a human works). --all also covers closed
    # (Completed/Cancelled) for history. Open = not Completed/Cancelled.
    st = cfg.choices.req_status
    flt = ("" if args.all else
           f"&$filter={p}status ne {st['Completed']} and {p}status ne {st['Cancelled']}")
    rows = dv.query(
        f"{p}inforequests?$select={p}inforequestid,{p}name,{p}category,{p}receiveddate,"
        f"_{p}contact_value,_{p}assigneerecommended_value"
        f"&$expand={p}sourcesignal($select={p}conversationid)"
        f"{flt}&$orderby=createdon desc&$top={args.limit}")
    done = skipped = 0
    for r in rows:
        rid = r[f"{p}inforequestid"]
        body = {}
        # §4.5 status inference — computed on every pass (traffic changes over time)
        conv = (r.get(f"{p}sourcesignal") or {}).get(f"{p}conversationid")
        body.update(run._status_fields(conv))
        # recommendations + draft — only when missing (unless --force)
        if not r.get(f"_{p}assigneerecommended_value") or args.force:
            code = val2code.get(r.get(f"{p}category"))
            cid = r.get(f"_{p}contact_value")
            ts_raw = r.get(f"{p}receiveddate")
            ts = (datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) if ts_raw
                  else datetime(2026, 1, 1, tzinfo=timezone.utc))
            rfi = RfiResult(is_info_request=True, category=code, confidence=80,
                            description=r.get(f"{p}name") or "")
            msg = {"subject": r.get(f"{p}name") or "", "_ts": ts}
            tier = ((run._routing_maps()[0].get(code) or {}).get("tier")) or "T1"
            if tier in cfg.choices.drafttier:
                body[f"{p}drafttier"] = cfg.choices.drafttier[tier]
            body.update(run._assignee_fields(rfi, msg, cid))
            body.update(run._draft_fields(rfi, msg, cid, tier))
        if not body:
            skipped += 1
            continue
        fields = sorted({k.split("@")[0].replace(p, "") for k in body})
        print(f"  {rid[:8]} -> {', '.join(fields)}")
        dv.patch(f"{p}inforequests", rid, body, f"backfill {rid[:8]}")
        done += 1

    print(f"\n{'patched' if args.apply else 'would patch'}: {done}   skipped: {skipped}")


if __name__ == "__main__":
    main()
