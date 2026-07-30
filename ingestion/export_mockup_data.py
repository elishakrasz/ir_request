"""Generate Tokens/data/dashboard-data.js for Exigent_IR_Dashboard_Mockup.html.

The mockup is a DISPLAY LAYER (brief's golden rule): this script is the data
seam. Sources, all real:
- Planner/SharePoint/process flags → ddm-web/data/tracker-data.json (the
  prospect tracker's 4-source join; file mtime = planner/sharepoint asOf)
- Dynamics → PROD Web API (live opps: owner, estimatedvalue, prospect code;
  engagement signals → we-owe / waiting-on)
- Carta → the tracker's `committed` amounts (Carta join is authoritative)

Emits `window.DASHBOARD = {...}` as a PLAIN script (not an ES module) so the
mockup runs from file:// with no server — deviation from the brief's `export`,
noted here and in the file header.

Usage: python -m ingestion.export_mockup_data
"""
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .dataverse_client import DataverseClient
from .sync import build_scope, parse_ts, say

TRACKER = Path("/home/emallard/Tokens/ddm-web/data/tracker-data.json")
OUT = Path("/home/emallard/Tokens/data/dashboard-data.js")
FMT = "@OData.Community.Display.V1.FormattedValue"
STAGES = [  # exactly the Planner "SubDocs Invite List" buckets (brief Step 1)
    ("Intake", "New Prospect / Intake"),
    ("NDA", "NDA Status"),
    ("Outreach", "Outreach / Meetings"),
    ("SubDocs", "SubDocs"),
    ("Completed", "Completed"),
]


def yn(v):
    return v == "y"


def money(v):
    m = re.sub(r"[^0-9.]", "", str(v or ""))
    return float(m) if m else 0.0


def stage_of(r, two_way_emails=frozenset()):
    """Stage from tracker flags, upgraded by ENGAGEMENT EVIDENCE: a prospect
    with real two-way correspondence (from the engagement-signal table) counts
    as Outreach even when the Planner teamsMeeting flag was never ticked —
    fixes the Outreach=0 artifact without demanding Planner discipline."""
    if r.get("status") == "CounterSigned" or yn(r.get("subdocsCompleted")):
        return "Completed"
    if yn(r.get("subdocsSent")) or r.get("status") in ("Invited", "InProgress"):
        return "SubDocs"
    if yn(r.get("teamsMeeting")) or yn(r.get("transcript")) \
            or (r.get("email") or "").lower() in two_way_emails:
        return "Outreach"
    if any(yn(r.get(k)) for k in ("sendNda", "nda", "ndaSigned", "ndaApproved",
                                  "sentDocusign")):
        return "NDA"
    return "Intake"


def main():
    cfg = Config.from_env()
    p, ch = cfg.prefix, cfg.choices
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=False)
    now = datetime.now(timezone.utc)
    tracker_rows = json.loads(TRACKER.read_text())
    tracker_mtime = datetime.fromtimestamp(TRACKER.stat().st_mtime, tz=timezone.utc)

    # ── Dynamics: live opps with owner + estimated revenue ───────────────────
    say("querying Dynamics (live opps + signals)")
    opp_rows = dv.query(
        "opportunities?$select=opportunityid,name,new_live,new_prospectcode,"
        "estimatedvalue,_ownerid_value,_parentcontactid_value"
        "&$filter=statecode eq 0 and new_live eq true")
    by_code = {(o.get("new_prospectcode") or "").strip(): o for o in opp_rows}
    owner_by_contact = {}
    for o in opp_rows:
        if o.get("_parentcontactid_value"):
            owner_by_contact[o["_parentcontactid_value"]] = \
                o.get(f"_ownerid_value{FMT}") or "—"
    live_cids = set(owner_by_contact)
    conn_rows = dv.fetch_connections()
    live_opp_ids = {o["opportunityid"] for o in opp_rows}
    for c in conn_rows:
        cid, oid = ((c.get("_record1id_value"), c.get("_record2id_value"))
                    if c.get("record1objecttypecode") == 2
                    else (c.get("_record2id_value"), c.get("_record1id_value")))
        if cid and oid in live_opp_ids:
            live_cids.add(cid)
            owner_by_contact.setdefault(
                cid, next((o.get(f"_ownerid_value{FMT}") for o in opp_rows
                           if o["opportunityid"] == oid), "—"))

    # ── engagement signals → we-owe / waiting-on (distinct contacts) ─────────
    sig_rows = dv.query(
        f"{p}engagementsignals?$select={p}direction,{p}timestamputc,"
        f"{p}responselatencymin,{p}ismeaningful,{p}matchstatus,{p}name,"
        f"_{p}contact_value,_{p}opportunity_value"
        f"&$filter={p}ismeaningful eq true and "
        f"{p}matchstatus ne {ch.matchstatus['Excluded']}")
    by_contact = defaultdict(list)
    for r in sig_rows:
        cid = r.get(f"_{p}contact_value")
        if cid in live_cids and r.get(f"{p}timestamputc"):
            by_contact[cid].append(r)
    cmeta = {c["contactid"]: c for c in dv.fetch_contacts(sorted(by_contact))}
    inb, outb = ch.direction["Inbound"], ch.direction["Outbound"]
    we_owe, waiting_on = [], []
    for cid, rows in by_contact.items():
        rows.sort(key=lambda r: r[f"{p}timestamputc"])
        last = rows[-1]
        ts = parse_ts(last[f"{p}timestamputc"])
        age = (now - ts).days
        name = (cmeta.get(cid) or {}).get("fullname") or "?"
        company = ((cmeta.get(cid) or {}).get("emailaddress1") or "").split("@")[-1]
        oppn = last.get(f"_{p}opportunity_value{FMT}") or ""
        entry = {
            "contact": name, "company": company, "opportunity": oppn,
            "owner": owner_by_contact.get(cid, "—"), "ageDays": age,
            "dynamicsUrl": f"{cfg.dataverse_url}/main.aspx?etn=contact"
                           f"&id={cid}&pagetype=entityrecord",
        }
        if last.get(f"{p}direction") == inb and \
                last.get(f"{p}responselatencymin") is None:
            we_owe.append({**entry, "lastInboundUtc": ts.isoformat()})
        elif last.get(f"{p}direction") == outb and age >= 2:
            waiting_on.append({**entry, "lastOutboundUtc": ts.isoformat()})
    we_owe.sort(key=lambda e: -e["ageDays"])
    waiting_on.sort(key=lambda e: -e["ageDays"])

    # engagement evidence for the Outreach stage (both directions observed)
    two_way_emails = set()
    for cid, rows in by_contact.items():
        dirs = {r.get(f"{p}direction") for r in rows}
        if inb in dirs and outb in dirs:
            for f in ("emailaddress1", "emailaddress2", "emailaddress3"):
                e = ((cmeta.get(cid) or {}).get(f) or "").strip().lower()
                if e:
                    two_way_emails.add(e)

    # ── prospects (Step 3 flags — all real from the tracker join) ────────────
    prospects = []
    for r in tracker_rows:
        code = (r.get("code") or "").strip()
        opp = by_code.get(code, {})
        prospects.append({
            "name": r.get("name") or "?",
            "opportunity": opp.get("name") or code or "—",
            "owner": (opp.get(f"_ownerid_value{FMT}") or "—"),
            "stage": stage_of(r, two_way_emails),
            "hasPlannerTask": r.get("badge") != "not in planner",
            "hasDynamicsOpp": bool(opp) or yn(r.get("trackedDynamics")),
            "folderExists": yn(r.get("ddm")) or yn(r.get("buzzMaterials")),
            "materialsWatermarked": yn(r.get("buzzMaterials")),
            "ndaSent": yn(r.get("sendNda")) or yn(r.get("sentDocusign")),
            "ndaReturned": yn(r.get("ndaSigned")),
            "ndaApproved": yn(r.get("ndaApproved")),
            "ndaLinkedInDynamics": yn(r.get("nda")),
            "subDocsSent": yn(r.get("subdocsSent"))
                or r.get("status") in ("Invited", "InProgress", "CounterSigned"),
            "subDocsCompleted": yn(r.get("subdocsCompleted"))
                or r.get("status") == "CounterSigned",
            "accompanyingEmailLogged": yn(r.get("sentEmail")),
            "commitmentIndicated": yn(r.get("commitIndicated")),
            "ndaReturnedAgeDays": int(r.get("days")) if str(r.get("days", "")
                                                           ).isdigit() else 0,
            "committedUsd": money(r.get("committed")),
            "estimatedUsd": float(opp.get("estimatedvalue") or 0),
        })

    # ── funnel + commitments ─────────────────────────────────────────────────
    # Funnel = PROGRESSION (brief's sample is monotonic): count of prospects
    # whose furthest stage is AT OR BEYOND each step, so conversions are
    # stage-to-stage survival rates. atStage kept for the register filter.
    stage_idx = {s: i for i, (s, _) in enumerate(STAGES)}
    at_stage = defaultdict(int)
    for pr in prospects:
        at_stage[pr["stage"]] += 1
    funnel = []
    for i, (s, b) in enumerate(STAGES):
        reached = sum(n for st, n in at_stage.items() if stage_idx[st] >= i)
        funnel.append({"stage": s, "count": reached, "atStage": at_stage[s],
                       "plannerBucket": b})

    tiers = defaultdict(float)
    by_stage = defaultdict(lambda: defaultdict(float))
    for pr in prospects:
        st = pr["stage"]
        if pr["committedUsd"]:
            tiers["finalCarta"] += pr["committedUsd"]
            by_stage[st]["final"] += pr["committedUsd"]
        elif pr["subDocsSent"]:
            tiers["written"] += pr["estimatedUsd"]
            by_stage[st]["written"] += pr["estimatedUsd"]
        elif pr["commitmentIndicated"]:
            tiers["verbal"] += pr["estimatedUsd"]
            by_stage[st]["verbal"] += pr["estimatedUsd"]
        else:
            tiers["soft"] += pr["estimatedUsd"]
            by_stage[st]["soft"] += pr["estimatedUsd"]
    est_total = sum(pr["estimatedUsd"] for pr in prospects)

    dashboard = {
        "meta": {"asOf": {
            "dynamics": now.isoformat(),
            "planner": tracker_mtime.isoformat(),
            "sharepoint": tracker_mtime.isoformat(),
            "carta": tracker_mtime.isoformat(),   # tracker's Carta join time
        }},
        "funnel": funnel,
        "awaitingReply": {
            "thresholdsDays": {"amber": 4, "red": 8},
            "weOwe": we_owe, "waitingOn": waiting_on,
        },
        "prospects": prospects,
        "commitments": {
            "byConfidence": {k: round(tiers[k]) for k in
                             ("soft", "verbal", "written", "finalCarta")},
            "varianceEstVsFinal": round(tiers["finalCarta"] - est_total),
            "byStage": [{"stage": s, **{t: round(by_stage[s][t]) for t in
                                        ("soft", "verbal", "written", "final")}}
                        for s, _ in STAGES],
        },
        "exclusions": {"note": "handled upstream by the engagement noise gate "
                               "(ingestion/rules.json noise_domains)"},
    }

    # Estimated-Revenue gap worklist (finding 2): opps the team should price.
    # Fill the amount column and feed it to ingestion/set_estimates.py.
    import csv as _csv
    gaps = OUT.parent / "estimated_revenue_gaps.csv"
    with gaps.open("w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["code", "prospect", "opportunity", "owner",
                    "estimated_usd_FILL_ME"])
        for pr in prospects:
            if not pr["estimatedUsd"] and not pr["committedUsd"] \
                    and pr["opportunity"] != "—":
                w.writerow([next((c for c, o in by_code.items()
                                  if (o.get("name") or "") == pr["opportunity"]),
                                 ""),
                            pr["name"], pr["opportunity"], pr["owner"], ""])
    say(f"wrote {gaps} (Estimated Revenue worklist)")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(
        "// GENERATED by ir_dashboard/ingestion/export_mockup_data.py — do not\n"
        "// hand-edit. Plain script (not an ES module) so the mockup runs from\n"
        "// file:// . Re-run the generator to refresh.\n"
        "// TODO: bind to Power Automate / Dataverse connector for auto-refresh.\n"
        f"window.DASHBOARD = {json.dumps(dashboard, indent=2)};\n")
    say(f"wrote {OUT} — funnel {[f['count'] for f in funnel]}, "
        f"weOwe {len(we_owe)}, waitingOn {len(waiting_on)}, "
        f"prospects {len(prospects)}")


if __name__ == "__main__":
    main()
