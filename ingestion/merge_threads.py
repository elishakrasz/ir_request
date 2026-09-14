"""One-time thread cleanup: fold follow-up tickets into the first open ticket
on their thread — gated by the same classifier the ingest-time merge uses.

Before the merge gate (sync._try_merge_request, 2026-09-10) every follow-up on
a thread opened its own ticket: Victoria Coates carried 6 open tickets from a
single scheduling conversation. This pass heals what was already fragmented.

Per thread with more than one OPEN ticket, the earliest-received ticket is the
one kept; each later ticket is put to the classifier against it ("same ask,
or a new one?") using the later ticket's title and its source email snippet.
Only a yes at or above MERGE_MIN_CONFIDENCE is folded: status -> Cancelled,
modifiedbyhint = "merge|into:<kept id>|<conf>%|<iso>". Anything else stays —
a request swallowed inside an unrelated ticket is lost work; a duplicate is a
tidiness problem. Measured 2026-09-10: about half of such tickets merge, half
are genuinely distinct asks that merely share a thread.

Idempotent: cancelled tickets are no longer open, so a re-run finds nothing.
Dry-run default; --apply writes. --mailbox X limits to threads that arrived
through that mailbox (repeatable).

Usage:  venv/bin/python -m ingestion.merge_threads [--apply] [--mailbox ir@...]
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone

from . import llm
from .config import Config
from .dataverse_client import DataverseClient
from .sync import clip, say

FMT = "@OData.Community.Display.V1.FormattedValue"
OPEN_STATUSES = ("New", "InProgress", "WaitingInternal", "WaitingExternal")


def plan_thread_merges(threads, judge, min_conf):
    """Pure. threads: iterable of lists of tickets, each ticket a dict with
    id, title, category, snippet, received. judge(subject, body, open_title,
    open_category) -> {"same_request", "confidence", "reason"} | None.
    Returns [(cancel_id, keep_id, confidence, reason)] — kept tickets are
    simply absent. The earliest-received ticket of a thread is always kept."""
    plan = []
    for tickets in threads:
        if len(tickets) < 2:
            continue
        ordered = sorted(tickets, key=lambda t: t.get("received") or "9999")
        head, rest = ordered[0], ordered[1:]
        for t in rest:
            v = judge(t.get("title") or "", t.get("snippet") or "",
                      head.get("title") or "", head.get("category") or "")
            if not v or not v.get("same_request"):
                continue
            conf = max(0, min(100, int(v.get("confidence") or 0)))
            if conf < min_conf:
                continue
            plan.append((t["id"], head["id"], conf, str(v.get("reason") or "")))
    return plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--mailbox", action="append",
                    help="only threads that arrived through this mailbox (repeatable)")
    args = ap.parse_args()
    cfg = Config.from_env()
    p, ch = cfg.prefix, cfg.choices
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=args.apply)
    only = {m.strip().lower() for m in (args.mailbox or []) if m.strip()}
    open_vals = {ch.req_status[k] for k in OPEN_STATUSES}

    say("pulling open requests with their source signals…")
    reqs = dv.query(
        f"{p}inforequests?$select={p}name,{p}status,{p}category,{p}receiveddate,"
        f"_{p}contact_value,_{p}sourcesignal_value"
        f"&$expand={p}sourcesignal($select={p}conversationid,{p}snippet,{p}provenance)")

    threads, meta = defaultdict(list), {}
    for r in reqs:
        if r.get(f"{p}status") not in open_vals:
            continue
        s = r.get(f"{p}sourcesignal") or {}
        conv = s.get(f"{p}conversationid")
        if not conv:
            continue
        mb = (s.get(f"{p}provenance") or "").split("|")[0].lower()
        if only and mb not in only:
            continue
        rid = r[f"{p}inforequestid"]
        threads[conv].append({
            "id": rid, "title": r.get(f"{p}name") or "",
            "category": r.get(f"{p}category{FMT}") or "",
            "snippet": s.get(f"{p}snippet") or "",
            "received": r.get(f"{p}receiveddate") or "",
        })
        meta[rid] = (r.get(f"_{p}contact_value{FMT}") or "?", mb)

    multi = [t for t in threads.values() if len(t) > 1]
    say(f"{len(multi)} open thread(s) carry more than one ticket "
        f"({sum(len(t) - 1 for t in multi)} follow-up ticket(s) to judge)")

    def judge(subject, body, open_title, open_category):
        return llm.same_request(subject, body, open_title, open_category, log=say)

    plan = plan_thread_merges(multi, judge, cfg.merge_min_confidence)
    to_cancel = {c: (k, conf, why) for c, k, conf, why in plan}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for tickets in sorted(multi, key=len, reverse=True):
        ordered = sorted(tickets, key=lambda t: t.get("received") or "9999")
        who, mb = meta[ordered[0]["id"]]
        say("")
        say(f"{who} — {len(tickets)} open · via {mb}")
        say(f"  KEEP     {ordered[0]['title'][:70]}")
        for t in ordered[1:]:
            if t["id"] in to_cancel:
                k, conf, why = to_cancel[t["id"]]
                say(f"  MERGE {conf:3d}%  {t['title'][:64]}")
                dv.patch(f"{p}inforequests", t["id"],
                         {f"{p}status": ch.req_status["Cancelled"],
                          f"{p}modifiedbyhint": clip(f"merge|into:{k}|{conf}%|{now}", 200)},
                         f"merge {t['id'][:8]} into {k[:8]} ({conf}%)")
            else:
                say(f"  SEPARATE  {t['title'][:64]}")

    kept = sum(len(t) - 1 for t in multi) - len(plan)
    say("")
    say(("APPLY: " if args.apply else "DRY-RUN (nothing written): ")
        + f"{len(plan)} follow-up ticket(s) merged, {kept} kept as distinct asks, "
        f"across {len(multi)} thread(s)")


if __name__ == "__main__":
    main()
