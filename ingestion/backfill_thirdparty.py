"""Backfill tickets for IR-route asks the third-party rule suppressed.

Until 2026-09-15 the IR route opened no ticket when the classifier judged the
sender a third party (advisor / bank / custodian). THIRD_PARTY_TICKETS=1 now
opens them at ingest time; this pass creates the ones already ingested as
signals — inbound, classifier said request, rfistatus Open, IR mailbox, no
ticket of their own, and no OPEN ticket on the thread (those were merges, not
suppressions). One ticket per message, whichever contact rows it has.

Uses SyncRun._create_rfi with the classifier re-run on the stored snippet, so
the ticket is what the route would have written: aigenerated, thirdparty=true,
due date from receipt. Idempotent: a signal with a ticket is never a candidate.

Usage:  venv/bin/python -m ingestion.backfill_thirdparty [--apply] [--since ISO]
"""
import argparse
from datetime import datetime, timezone

from .classify import classify
from .config import Config, load_env
from .dataverse_client import DataverseClient
from .graph_client import GraphClient
from .sync import SyncRun, code_version, parse_ts, say

FMT = "@OData.Community.Display.V1.FormattedValue"
IR = ("ir@exigentcap.com", "mravid@exigentcap.com", "lgruber@exigentcap.com",
      "kbardash@exigentcap.com", "ereinhard@exigentcap.com")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--since", default="2026-09-10T00:00:00Z")
    args = ap.parse_args()
    env = load_env(); cfg = Config.from_env(env)
    p, ch = cfg.prefix, cfg.choices
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=args.apply)
    open_vals = {ch.req_status[k] for k in ("New", "InProgress", "WaitingInternal",
                                            "WaitingExternal")}

    sigs = dv.query(
        f"{p}engagementsignals?$select={p}name,{p}sender,{p}timestamputc,{p}provenance,"
        f"{p}conversationid,{p}messagekeyhash,{p}snippet,_{p}contact_value,"
        f"_{p}opportunity_value&$filter={p}isinforequest eq true and "
        f"{p}direction eq {ch.direction['Inbound']} and {p}rfistatus eq {ch.rfistatus['Open']}"
        f" and {p}ismeaningful eq true and {p}timestamputc ge {args.since}"
        f"&$orderby={p}timestamputc desc")
    sigs = [s for s in sigs if (s.get(f"{p}provenance") or "").split("|")[0] in IR]
    reqs = dv.query(f"{p}inforequests?$select={p}status,_{p}sourcesignal_value")
    ticketed = {r.get(f"_{p}sourcesignal_value") for r in reqs}
    open_by_sig = {r.get(f"_{p}sourcesignal_value") for r in reqs
                   if r[f"{p}status"] in open_vals}

    run = SyncRun(cfg, GraphClient(cfg.tenant_id, cfg.client_id, cfg.client_secret),
                  dv, apply=args.apply, codeversion=code_version())
    # One message × N matched contacts is N rows. The ticket must sit on the
    # investor, not on a Carta robot that happened to be on the thread: prefer
    # the row whose contact holds the sender address, then any non-Carta
    # contact, then whatever is left.
    contact_email = {}
    cids = sorted({s.get(f"_{p}contact_value") for s in sigs if s.get(f"_{p}contact_value")})
    for c in dv.fetch_contacts(cids):
        contact_email[c["contactid"]] = {(c.get(f) or "").lower()
                                         for f in ("emailaddress1", "emailaddress2", "emailaddress3")}

    def rank(s):
        cid, sender = s.get(f"_{p}contact_value"), (s.get(f"{p}sender") or "").lower()
        name = (s.get(f"_{p}contact_value{FMT}") or "").lower()
        return (0 if sender in contact_email.get(cid, set()) else
                2 if "carta" in name else 1)

    by_hash = {}
    for s in sigs:
        by_hash.setdefault(s.get(f"{p}messagekeyhash"), []).append(s)
    ordered = [sorted(rows, key=rank)[0] for rows in by_hash.values()]
    ordered.sort(key=lambda s: s[f"{p}timestamputc"], reverse=True)

    seen, made, skipped = set(), 0, 0
    for s in ordered:
        sid, h = s[f"{p}engagementsignalid"], s.get(f"{p}messagekeyhash")
        if h in seen or any(r[f"{p}engagementsignalid"] in ticketed for r in by_hash[h]):
            continue
        seen.add(h)
        conv = s.get(f"{p}conversationid") or ""
        if conv:
            ids = [c[f"{p}engagementsignalid"] for c in dv.query(
                f"{p}engagementsignals?$select={p}engagementsignalid"
                f"&$filter={p}conversationid eq '{conv}'")]
            if any(i in open_by_sig for i in ids):
                skipped += 1
                say(f"skip (thread has an open ticket): {(s.get(f'{p}name') or '')[:60]}")
                continue
        rfi = classify(s.get(f"{p}name") or "", s.get(f"{p}snippet") or "")
        if not rfi.is_info_request:
            skipped += 1
            say(f"skip (classifier no longer calls it a request): "
                f"{(s.get(f'{p}name') or '')[:60]}")
            continue
        msg = {"subject": s.get(f"{p}name") or "", "_ts": parse_ts(s[f"{p}timestamputc"]),
               "_hash": h or sid}
        who = s.get(f"_{p}contact_value{FMT}") or "?"
        say(f"CREATE  {who:28s} [{rfi.category}] "
            f"{(rfi.description or msg['subject'])[:64]}{'  (third-party)' if rfi.third_party else ''}")
        run._create_rfi({f"{p}engagementsignalid": sid}, msg, rfi,
                        s.get(f"_{p}contact_value"), s.get(f"_{p}opportunity_value"))
        made += 1
    say(("APPLY: " if args.apply else "DRY-RUN (nothing written): ")
        + f"{made} ticket(s) created, {skipped} skipped")


if __name__ == "__main__":
    main()
