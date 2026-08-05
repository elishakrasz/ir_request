"""One-time dedupe: the same email matched to N contacts created N twin
requests (found live 2026-08-05 — e.g. one Paul Lapping message opened
tickets under both Ray Jordan and Paul Farrell). Groups OPEN requests by the
source signal's messagekeyhash and cancels all but one.

Keeper preference: human-assigned first, then the source signal marked
is_primary, then earliest received. Cancelled twins get
modifiedbyhint = "dedupe|dup-of:<kept id>|<iso>" — auditable, not deleted.

sync.py now creates ONE request per message, so this should stay clean;
re-running is safe (idempotent — twins already Cancelled are skipped).

Dry-run default; --apply writes.
Usage:  venv/bin/python -m ingestion.dedupe_requests [--apply]
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone

from .config import Config
from .dataverse_client import DataverseClient
from .sync import say

FMT = "@OData.Community.Display.V1.FormattedValue"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    cfg = Config.from_env()
    p, ch = cfg.prefix, cfg.choices
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=args.apply)

    open_vals = {ch.req_status[k] for k in
                 ("New", "InProgress", "WaitingInternal", "WaitingExternal")}
    reqs = dv.query(f"{p}inforequests?$select={p}name,{p}status,"
                    f"{p}receiveddate,_ownerid_value,_{p}sourcesignal_value")
    sigs = dv.query(f"{p}engagementsignals?$select={p}messagekeyhash,"
                    f"{p}isprimary")
    sig_info = {s[f"{p}engagementsignalid"]:
                (s.get(f"{p}messagekeyhash"), s.get(f"{p}isprimary"))
                for s in sigs}

    groups = defaultdict(list)
    for r in reqs:
        if r.get(f"{p}status") not in open_vals:
            continue
        sid = r.get(f"_{p}sourcesignal_value")
        h, is_prim = sig_info.get(sid, (None, None))
        if not h:
            continue
        owner = r.get(f"_ownerid_value{FMT}", "")
        r["_assigned"] = bool(owner) and not owner.lstrip().startswith("#") \
            and "pipeline" not in owner.lower()
        r["_isprim"] = bool(is_prim) or is_prim is None
        groups[h].append(r)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cancelled = 0
    for h, rows in groups.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: (not r["_assigned"], not r["_isprim"],
                                 r.get(f"{p}receiveddate") or "9999"))
        keep, dups = rows[0], rows[1:]
        say(f"KEEP  {(keep.get(f'{p}name') or '')[:60]}")
        for d in dups:
            say(f"  cancel twin: {(d.get(f'{p}name') or '')[:60]}")
            dv.patch(f"{p}inforequests", d[f"{p}inforequestid"],
                     {f"{p}status": ch.req_status["Cancelled"],
                      f"{p}modifiedbyhint":
                          f"dedupe|dup-of:{keep[f'{p}inforequestid']}|{now}"},
                     f"dedupe {d[f'{p}inforequestid'][:8]}")
            cancelled += 1

    say(("APPLY: " if args.apply else "DRY-RUN (nothing written): ") +
        f"{cancelled} twin(s) cancelled across "
        f"{sum(1 for r in groups.values() if len(r) > 1)} duplicated message(s)")


if __name__ == "__main__":
    main()
