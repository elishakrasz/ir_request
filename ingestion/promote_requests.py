"""§0.2 backfill: promote RFI-flagged signals into the Information Request
table.

Historical signals carry rfistatus Open/Answered but predate the WS6 wiring
(and its _create_rfi bug), so the request table is empty. This one-time,
idempotent pass creates the missing records:

  - keyed on the Source Signal lookup — re-running creates nothing new
  - Open signal    → request status New, due = explicit deadline else
                     received + RFI_DUE_BDAYS business days (Sun–Thu)
  - Answered signal→ request status Completed, completeddate = the earliest
                     later outbound in the same conversation (aging analytics
                     keep the record)
  - title/routing/category/urgency from one cached Opus call per signal
    (llm.classify_promotion); routing lands in state/request_routing.json
    (4-way taxonomy has no Dataverse column yet — see provision.py note)

Dry-run default; --apply writes. Going forward, sync.py promotes new inbound
at ingestion time — this module only heals history.

Usage:  venv/bin/python -m ingestion.promote_requests [--apply]
"""
import argparse
import json
from collections import Counter, defaultdict

from . import llm
from .config import Config, STATE_DIR
from .dataverse_client import DataverseClient
from .sync import URGENCY_KEY, add_business_days, clip, parse_ts, say

ROUTING_FILE = STATE_DIR / "request_routing.json"


def load_routing() -> dict:
    if ROUTING_FILE.exists():
        return json.loads(ROUTING_FILE.read_text())
    return {}


def save_routing(routing: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ROUTING_FILE.write_text(json.dumps(routing, indent=0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cfg = Config.from_env()
    ch, p = cfg.choices, cfg.prefix
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=args.apply)

    say("pulling signals…")
    sigs = dv.query(
        f"{p}engagementsignals?$select={p}name,{p}direction,{p}timestamputc,"
        f"{p}snippet,{p}conversationid,{p}rfistatus,{p}matchstatus,"
        f"{p}ismeaningful,_{p}contact_value,_{p}opportunity_value")

    # conversation → sorted outbound timestamps (for Answered completion ts)
    conv_out = defaultdict(list)
    for s in sigs:
        if s.get(f"{p}direction") == ch.direction["Outbound"] \
                and s.get(f"{p}conversationid") and s.get(f"{p}timestamputc"):
            conv_out[s[f"{p}conversationid"]].append(parse_ts(s[f"{p}timestamputc"]))
    for v in conv_out.values():
        v.sort()

    say("pulling existing requests…")
    existing = dv.query(f"{p}inforequests?$select=_{p}sourcesignal_value")
    promoted = {r.get(f"_{p}sourcesignal_value") for r in existing}

    excluded = ch.matchstatus["Excluded"]
    todo = [s for s in sigs
            if s.get(f"{p}rfistatus") in (ch.rfistatus["Open"],
                                          ch.rfistatus["Answered"])
            and s.get(f"{p}direction") == ch.direction["Inbound"]
            and s.get(f"{p}ismeaningful")
            and s.get(f"{p}matchstatus") != excluded
            and s.get(f"_{p}contact_value")
            and s[f"{p}engagementsignalid"] not in promoted]

    by_status = Counter("Open" if s[f"{p}rfistatus"] == ch.rfistatus["Open"]
                        else "Answered" for s in todo)
    by_opp = Counter(s.get(f"_{p}opportunity_value{'@OData.Community.Display.V1.FormattedValue'}",
                           "(no opp)") for s in todo)
    say(f"{len(todo)} signal(s) to promote ({dict(by_status)}); "
        f"already promoted: {len(promoted - {None})}")
    for opp, n in by_opp.most_common(10):
        say(f"  {n:3d}  {opp}")

    routing = load_routing()
    made = Counter()
    for s in todo:
        sid = s[f"{p}engagementsignalid"]
        subject = s.get(f"{p}name") or "(no subject)"
        snippet = s.get(f"{p}snippet") or ""
        ts = parse_ts(s[f"{p}timestamputc"])
        is_open = s[f"{p}rfistatus"] == ch.rfistatus["Open"]

        promo = llm.classify_promotion(subject, snippet, log=say) or {}
        title = promo.get("title") or subject
        deadline = None
        if promo.get("urgency") == "explicit_deadline" and promo.get("deadline"):
            try:
                deadline = parse_ts(promo["deadline"]).date().isoformat()
            except ValueError:
                pass
        due = parse_ts(deadline) if deadline else add_business_days(
            ts, cfg.rfi_due_bdays)

        body = {
            f"{p}name": clip(title, 200),
            f"{p}status": ch.req_status["New" if is_open else "Completed"],
            f"{p}category": ch.req_category.get(promo.get("category") or "Other",
                                                ch.req_category["Other"]),
            f"{p}receiveddate": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            f"{p}duedate": due.strftime("%Y-%m-%dT%H:%M:%SZ"),
            f"{p}statedurgency": ch.urgency[
                URGENCY_KEY.get(promo.get("urgency") or "none", "None")],
            f"{p}aigenerated": True,
            f"{p}humanconfirmed": False,
            f"{p}contact@odata.bind": f"/contacts({s[f'_{p}contact_value']})",
            f"{p}sourcesignal@odata.bind": f"/{p}engagementsignals({sid})",
        }
        if deadline:
            body[f"{p}explicitdeadline"] = deadline
        rk = {"process_blocker": "ProcessBlocker", "conviction": "Conviction",
              "deal_mechanics": "DealMechanics", "scheduling": "Scheduling"} \
            .get(promo.get("routing_category"))
        if rk:                       # routing column live in PROD since 08-03
            body[f"{p}routingcategory"] = ch.routing[rk]
        if s.get(f"_{p}opportunity_value"):
            body[f"{p}opportunity@odata.bind"] = \
                f"/opportunities({s[f'_{p}opportunity_value']})"
        if not is_open:
            reply = next((t for t in conv_out.get(s.get(f"{p}conversationid"), [])
                          if t > ts), None)
            if reply:
                body[f"{p}completeddate"] = reply.strftime("%Y-%m-%dT%H:%M:%SZ")

        dv.create(f"{p}inforequests", body,
                  f"{'open' if is_open else 'answered'} rfi · {title[:60]}")
        made["Open" if is_open else "Answered"] += 1
        if promo.get("routing_category"):
            routing[sid] = promo["routing_category"]

    if args.apply:
        save_routing(routing)
        say(f"APPLY done: {dict(made)} created "
            f"({dv.created} rows), routing sidecar: {len(routing)} entries")
    else:
        say(f"DRY-RUN: would create {dict(made)} request(s). "
            "Re-run with --apply to write.")


if __name__ == "__main__":
    main()
