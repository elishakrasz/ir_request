"""Backfill the close-readiness columns after the 2026-08-03 PROD import.

  new_routingcategory (Information Request)
      from state/request_routing.json — the sidecar written at promotion time
      while the column didn't exist. Idempotent: only patches rows where the
      column is empty.

  new_isprimary (Engagement Signal)
      §0.1 semantics: within (opportunity, messagekeyhash) the same message
      keeps ONE primary attribution; rows with no opportunity are unique per
      contact and stay primary. Convention: null/true = primary, false =
      duplicate — so the backfill only writes `false` on duplicates (and
      repairs any drift), keeping the patch count small. Re-runnable.

Dry-run default; --apply writes.
Usage:  venv/bin/python -m ingestion.backfill_close_columns [--apply]
"""
import argparse
import json
from collections import defaultdict

from . import llm
from .config import Config, STATE_DIR
from .dataverse_client import DataverseClient
from .sync import say

ROUTING_FILE = STATE_DIR / "request_routing.json"
# sidecar labels (snake_case, from llm.ROUTING_CATEGORIES) → choice keys
ROUTING_KEY = {"process_blocker": "ProcessBlocker", "conviction": "Conviction",
               "deal_mechanics": "DealMechanics", "scheduling": "Scheduling"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    cfg = Config.from_env()
    p, ch = cfg.prefix, cfg.choices
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=args.apply)

    # ── routing categories ──────────────────────────────────────────────────
    sidecar = json.loads(ROUTING_FILE.read_text()) if ROUTING_FILE.exists() else {}
    reqs = dv.query(f"{p}inforequests?$select={p}routingcategory,"
                    f"_{p}sourcesignal_value")
    r_patched = r_skipped = r_classified = 0
    for r in reqs:
        if r.get(f"{p}routingcategory") is not None:
            r_skipped += 1
            continue
        sig_id = r.get(f"_{p}sourcesignal_value")
        label = sidecar.get(sig_id)
        if not label and sig_id:
            # request created after promotion (fixed _create_rfi) — classify
            # its source signal now (cached Opus call, one-time)
            rows = dv.query(f"{p}engagementsignals?$select={p}name,{p}snippet"
                            f"&$filter={p}engagementsignalid eq {sig_id}")
            if rows:
                promo = llm.classify_promotion(
                    rows[0].get(f"{p}name") or "",
                    rows[0].get(f"{p}snippet") or "", log=say) or {}
                label = promo.get("routing_category")
                if label:
                    sidecar[sig_id] = label
                    r_classified += 1
        key = ROUTING_KEY.get(label)
        if not key:
            continue
        dv.patch(f"{p}inforequests", r[f"{p}inforequestid"],
                 {f"{p}routingcategory": ch.routing[key]},
                 f"routing {label} → {r[f'{p}inforequestid'][:8]}")
        r_patched += 1
    if args.apply and r_classified:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ROUTING_FILE.write_text(json.dumps(sidecar, indent=0))
    say(f"routing: {r_patched} patched ({r_classified} freshly classified), "
        f"{r_skipped} already set")

    # ── is-primary ──────────────────────────────────────────────────────────
    say("pulling signals…")
    sigs = dv.query(
        f"{p}engagementsignals?$select={p}messagekeyhash,{p}sender,"
        f"{p}isprimary,_{p}contact_value,_{p}opportunity_value")
    cids = sorted({s.get(f"_{p}contact_value") for s in sigs
                   if s.get(f"_{p}contact_value")})
    email_of = {}
    for i in range(0, len(cids), 20):
        flt = " or ".join(f"contactid eq {c}" for c in cids[i:i + 20])
        for c in dv.query(f"contacts?$select=contactid,emailaddress1,"
                          f"emailaddress2,emailaddress3&$filter={flt}"):
            # all three count: a contact reached on their 2nd/3rd address is
            # just as much the sender (scope map indexes all three too)
            email_of[c["contactid"]] = [
                (c.get(f) or "").strip().lower()
                for f in ("emailaddress1", "emailaddress2", "emailaddress3")
                if (c.get(f) or "").strip()]

    groups = defaultdict(list)
    for s in sigs:
        opp, cid = s.get(f"_{p}opportunity_value"), s.get(f"_{p}contact_value")
        h = s.get(f"{p}messagekeyhash") or s[f"{p}engagementsignalid"]
        groups[f"o|{opp}|{h}" if opp else f"c|{cid}|{h}"].append(s)

    to_false, to_true = [], []
    for rows in groups.values():
        def pref(s):
            cid = s.get(f"_{p}contact_value") or ""
            sender = (s.get(f"{p}sender") or "").lower()
            ems = email_of.get(cid) or []
            return (0 if any(e in sender for e in ems) else 1, cid)
        rows.sort(key=pref)
        for i, s in enumerate(rows):
            stored = s.get(f"{p}isprimary")          # None = unset = primary
            want_primary = i == 0
            if want_primary and stored is False:
                to_true.append(s)
            elif not want_primary and stored is not False:
                to_false.append(s)

    say(f"isprimary: {len(groups)} message groups from {len(sigs)} rows → "
        f"{len(to_false)} duplicates to mark false, {len(to_true)} to repair true")
    for s, val in [(s, False) for s in to_false] + [(s, True) for s in to_true]:
        dv.patch(f"{p}engagementsignals", s[f"{p}engagementsignalid"],
                 {f"{p}isprimary": val}, f"isprimary={val}")

    say(("APPLY done: " if args.apply else "DRY-RUN (nothing written): ") +
        f"routing {r_patched}, isprimary false {len(to_false)} / true {len(to_true)}")


if __name__ == "__main__":
    main()
