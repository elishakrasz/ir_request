"""Repair wrong-year deadlines on existing Information Requests.

The classifier sometimes emitted last year for 'by 8/15'-style deadlines
(found live: due 2025-08-15 on a request received 2026-07-31). For every
request whose explicit deadline or due date precedes its received date, bump
the year until plausible (sane_deadline) and patch both columns; drops that
can't be repaired are reported, not written.

Dry-run default; --apply writes.
Usage:  venv/bin/python -m ingestion.repair_deadlines [--apply]
"""
import argparse

from .config import Config
from .dataverse_client import DataverseClient
from .sync import parse_ts, sane_deadline, say


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    cfg = Config.from_env()
    p = cfg.prefix
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=args.apply)

    reqs = dv.query(f"{p}inforequests?$select={p}name,{p}receiveddate,"
                    f"{p}duedate,{p}explicitdeadline")
    fixed = dropped = 0
    for r in reqs:
        recv = r.get(f"{p}receiveddate")
        if not recv:
            continue
        received = parse_ts(recv)
        bad = []
        for col in (f"{p}explicitdeadline", f"{p}duedate"):
            v = r.get(col)
            if v and parse_ts(v) < received:
                bad.append(col)
        if not bad:
            continue
        patch = {}
        for col in bad:
            repaired = sane_deadline(r[col], received)
            if repaired:
                patch[col] = repaired if col.endswith("explicitdeadline") \
                    else f"{repaired}T17:00:00Z"
            else:
                patch[col] = None
                dropped += 1
        say(f"{(r.get(f'{p}name') or '')[:55]}: "
            + ", ".join(f"{c.removeprefix(p)} {r[c][:10]} → "
                        f"{str(patch[c])[:10]}" for c in bad))
        dv.patch(f"{p}inforequests", r[f"{p}inforequestid"], patch,
                 f"deadline repair {r[f'{p}inforequestid'][:8]}")
        fixed += 1

    say(("APPLY: " if args.apply else "DRY-RUN (nothing written): ") +
        f"{fixed} request(s) repaired, {dropped} implausible value(s) cleared")


if __name__ == "__main__":
    main()
