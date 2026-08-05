"""Export the full engagement dataset to a multi-sheet Excel workbook.

Read-only. Pulls everything the /engagement dashboard sees — signals, contacts,
opportunities, contact↔opportunity links, info requests — from Dataverse, plus
the prospect-tracker feed, and writes reports/engagement_export_<ts>.xlsx
(also copied to the Windows Downloads folder when reachable).

Run:  venv/bin/python -m ingestion.export_xlsx
"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .config import REPO, Config
from .dataverse_client import DataverseClient

FMT = "@OData.Community.Display.V1.FormattedValue"
TRACKER_JSON = REPO.parent / "Tokens" / "ddm-web" / "data" / "tracker-data.json"
WIN_DOWNLOADS = Path("/mnt/c/Users/emallard/Downloads")


def lab(row: dict, col: str, default=""):
    """Formatted label for a column when Dataverse provides one, else raw."""
    return row.get(f"{col}{FMT}", row.get(col, default))


def signals_frame(dv: DataverseClient, p: str) -> pd.DataFrame:
    rows = dv.query(
        f"{p}engagementsignals?$select={p}name,{p}channel,{p}direction,"
        f"{p}timestamputc,{p}sender,{p}snippet,{p}conversationid,{p}rfistatus,"
        f"{p}matchstatus,{p}matchmethod,{p}matchconfidence,{p}noisereason,"
        f"{p}responselatencymin,{p}ismeaningful,{p}sourcelink,createdon,"
        f"{p}messagekeyhash,_{p}contact_value,_{p}opportunity_value")
    out = []
    for r in rows:
        lat = r.get(f"{p}responselatencymin")
        out.append({
            "Timestamp (UTC)": r.get(f"{p}timestamputc"),
            "Direction": lab(r, f"{p}direction"),
            "Contact": lab(r, f"_{p}contact_value"),
            "Opportunity": lab(r, f"_{p}opportunity_value"),
            "Sender": r.get(f"{p}sender"),
            "Subject": r.get(f"{p}name"),
            "Snippet": r.get(f"{p}snippet"),
            "Match status": lab(r, f"{p}matchstatus"),
            "Match method": lab(r, f"{p}matchmethod"),
            "Confidence": r.get(f"{p}matchconfidence"),
            "Noise reason": r.get(f"{p}noisereason"),
            "Meaningful": bool(r.get(f"{p}ismeaningful")),
            "Response latency (biz min)": lat,
            "Response latency (biz hrs)": round(lat / 60, 1) if lat is not None else None,
            "RFI status": lab(r, f"{p}rfistatus"),
            "Conversation id": r.get(f"{p}conversationid"),
            "Source link": r.get(f"{p}sourcelink"),
            "Message key hash": r.get(f"{p}messagekeyhash") or "",
            "Contact id": r.get(f"_{p}contact_value"),
            "Opportunity id": r.get(f"_{p}opportunity_value"),
        })
    df = pd.DataFrame(out)
    return df.sort_values("Timestamp (UTC)", ascending=False) if len(df) else df


def requests_frame(dv: DataverseClient, p: str) -> pd.DataFrame:
    rows = dv.query(
        f"{p}inforequests?$select={p}name,{p}status,{p}category,{p}receiveddate,"
        f"{p}duedate,{p}completeddate,{p}statedurgency,{p}explicitdeadline,"
        f"{p}routingcategory,{p}aigenerated,{p}humanconfirmed,_{p}contact_value,"
        f"_{p}opportunity_value,_{p}sourcesignal_value")
    # stored choice label "ProcessBlocker" → analysis snake_case key
    snake = {"ProcessBlocker": "process_blocker", "Conviction": "conviction",
             "DealMechanics": "deal_mechanics", "Scheduling": "scheduling"}
    out = [{
        "Stored routing": snake.get(lab(r, f"{p}routingcategory", "")),
        "Received": r.get(f"{p}receiveddate"),
        "Title": r.get(f"{p}name"),
        "Status": lab(r, f"{p}status"),
        "Category": lab(r, f"{p}category"),
        "Contact": lab(r, f"_{p}contact_value"),
        "Opportunity": lab(r, f"_{p}opportunity_value"),
        "Due": r.get(f"{p}duedate"),
        "Completed": r.get(f"{p}completeddate"),
        "Stated urgency": lab(r, f"{p}statedurgency"),
        "Explicit deadline": r.get(f"{p}explicitdeadline"),
        "AI generated": bool(r.get(f"{p}aigenerated")),
        "Human confirmed": bool(r.get(f"{p}humanconfirmed")),
        "Source signal id": r.get(f"_{p}sourcesignal_value"),
    } for r in rows]
    return pd.DataFrame(out, columns=[
        "Received", "Title", "Status", "Category", "Contact", "Opportunity",
        "Due", "Completed", "Stated urgency", "Explicit deadline",
        "AI generated", "Human confirmed", "Source signal id",
        "Stored routing"])


def scope_frames(dv: DataverseClient, cfg: Config):
    p = cfg.prefix
    opps = dv.fetch_opportunities(cfg.fund_lookup)
    opp_df = pd.DataFrame([{
        "Opportunity": o.get("name"),
        "Fund": lab(o, f"_{cfg.fund_lookup}_value", "(no fund)"),
        "Live": bool(o.get("new_live")),
        "Prospect code": o.get("new_prospectcode") or "",
        "Monitored": bool(o.get(f"{p}activemonitoring")),
        "Monitoring start": o.get(f"{p}monitoringstartdate"),
        "Primary contact": lab(o, "_parentcontactid_value"),
        "Opportunity id": o.get("opportunityid"),
    } for o in opps]).sort_values(["Fund", "Opportunity"])

    opp_ids = {o["opportunityid"] for o in opps}
    opp_name = {o["opportunityid"]: o.get("name") for o in opps}
    links = []
    for o in opps:
        if o.get("_parentcontactid_value"):
            links.append((o["_parentcontactid_value"], o["opportunityid"], "Primary contact"))
    for c in dv.fetch_connections():
        cid, oid = ((c["_record1id_value"], c["_record2id_value"])
                    if c["record1objecttypecode"] == 2
                    else (c["_record2id_value"], c["_record1id_value"]))
        if cid and oid in opp_ids:
            links.append((cid, oid, "Connection"))

    contact_ids = sorted({cid for cid, _, _ in links})
    crows = []
    for i in range(0, len(contact_ids), 20):
        flt = " or ".join(f"contactid eq {c}" for c in contact_ids[i:i + 20])
        crows.extend(dv.query(
            "contacts?$select=contactid,fullname,emailaddress1,emailaddress2,"
            f"{p}aisummary,{p}aisummaryat&$filter={flt}"))
    cname = {c["contactid"]: c.get("fullname") or "(no name)" for c in crows}

    contact_df = pd.DataFrame([{
        "Contact": c.get("fullname") or "(no name)",
        "Email": c.get("emailaddress1") or c.get("emailaddress2") or "",
        "AI summary": c.get(f"{p}aisummary") or "",
        "AI summary at": c.get(f"{p}aisummaryat"),
        "Contact id": c["contactid"],
    } for c in crows]).sort_values("Contact")

    link_df = pd.DataFrame([{
        "Contact": cname.get(cid, cid),
        "Opportunity": opp_name.get(oid, oid),
        "Link type": kind,
        "Contact id": cid,
        "Opportunity id": oid,
    } for cid, oid, kind in links]).sort_values(["Contact", "Opportunity"])
    return opp_df, contact_df, link_df


def tracker_frame() -> pd.DataFrame:
    try:
        rows = json.loads(TRACKER_JSON.read_text())
    except OSError:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.sort_values("code") if "code" in df else df


def enrich_contacts(contact_df: pd.DataFrame, sig_df: pd.DataFrame) -> pd.DataFrame:
    if contact_df.empty or sig_df.empty:
        return contact_df
    g = sig_df.groupby("Contact id")
    stats = pd.DataFrame({
        "Signals": g.size(),
        "Inbound": g.apply(lambda d: int((d["Direction"] == "Inbound").sum()),
                           include_groups=False),
        "Outbound": g.apply(lambda d: int((d["Direction"] == "Outbound").sum()),
                            include_groups=False),
        "Last inbound": g.apply(
            lambda d: d.loc[d["Direction"] == "Inbound", "Timestamp (UTC)"].max(),
            include_groups=False),
        "Last outbound": g.apply(
            lambda d: d.loc[d["Direction"] == "Outbound", "Timestamp (UTC)"].max(),
            include_groups=False),
    })
    out = contact_df.merge(stats, left_on="Contact id", right_index=True, how="left")
    out["Signals"] = out["Signals"].fillna(0).astype(int)
    for c in ("Inbound", "Outbound"):
        out[c] = out[c].fillna(0).astype(int)
    cols = ["Contact", "Email", "Signals", "Inbound", "Outbound",
            "Last inbound", "Last outbound", "AI summary", "AI summary at",
            "Contact id"]
    return out[cols]


def write_blocks(xl, sheet: str, blocks):
    """Write [(caption, df), …] stacked on one sheet with blank separators."""
    row = 0
    for caption, df in blocks:
        pd.DataFrame({sheet: [caption]}).to_excel(
            xl, sheet_name=sheet, index=False, header=False, startrow=row)
        row += 1
        if df is None or df.empty:
            pd.DataFrame({" ": ["(none)"]}).to_excel(
                xl, sheet_name=sheet, index=False, header=False, startrow=row)
            row += 2
            continue
        df.to_excel(xl, sheet_name=sheet, index=False, startrow=row)
        row += len(df) + 3


def autofit(ws, max_width=60):
    for col in ws.columns:
        letter = col[0].column_letter
        width = max((len(str(c.value)) for c in col[:200] if c.value is not None),
                    default=8)
        ws.column_dimensions[letter].width = min(max(width + 2, 10), max_width)
    ws.freeze_panes = "A2"


def main():
    cfg = Config.from_env()
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, cfg.prefix, apply=False)
    p = cfg.prefix
    print("pulling signals…", flush=True)
    sig_df = signals_frame(dv, p)
    print(f"  {len(sig_df)} signals")
    print("pulling requests…", flush=True)
    req_df = requests_frame(dv, p)
    print("pulling scope (opps/contacts/links)…", flush=True)
    opp_df, contact_df, link_df = scope_frames(dv, cfg)
    contact_df = enrich_contacts(contact_df, sig_df)
    trk_df = tracker_frame()

    now = datetime.now(timezone.utc)

    # ── close-readiness analysis layer (directive 2026-08-02) ─────────────
    from . import analysis
    fund_of = dict(zip(opp_df["Opportunity id"], opp_df["Fund"]))
    email_of = dict(zip(contact_df["Contact id"], contact_df["Email"]))
    sig_df["Fund"] = sig_df["Opportunity id"].map(fund_of).fillna("(no fund)")
    sig_df["Contact email"] = sig_df["Contact id"].map(email_of).fillna("")
    sig_df = analysis.mark_primary(sig_df)                       # §0.1
    print("computing views…", flush=True)
    bic_work, bic_all = analysis.ball_in_court(sig_df, now)      # §1
    req_open = analysis.open_requests(req_df, now)               # §2
    clock = analysis.deal_clock(trk_df, bic_all, now)            # §3
    stages, stuck = analysis.funnel(trk_df, bic_all, req_open
                                    if isinstance(req_open, pd.DataFrame)
                                    else pd.DataFrame())         # §4
    lat_sum, lat_trend, lat_off = analysis.latency_tail(sig_df)  # §5
    hyg = analysis.hygiene(sig_df, contact_df, link_df, opp_df, now)  # §6
    summary = pd.DataFrame([
        ("Generated (UTC)", now.strftime("%Y-%m-%d %H:%M")),
        ("Environment", cfg.dataverse_url),
        ("Engagement signals", len(sig_df)),
        ("  Confirmed", int((sig_df["Match status"] == "Confirmed").sum())),
        ("  Suggested", int((sig_df["Match status"] == "Suggested").sum())),
        ("  Excluded (noise)", int((sig_df["Match status"] == "Excluded").sum())),
        ("Contacts in scope", len(contact_df)),
        ("Open opportunities", len(opp_df)),
        ("  Live", int(opp_df["Live"].sum())),
        ("Contact↔Opp links", len(link_df)),
        ("Info requests", len(req_df)),
        ("Prospect tracker rows", len(trk_df)),
    ], columns=["Metric", "Value"])

    out_dir = REPO / "reports"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"engagement_export_{now.strftime('%Y%m%d_%H%M')}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xl:
        summary.to_excel(xl, sheet_name="Summary", index=False)
        # analysis views first — these are the triage worklists
        bic_work.to_excel(xl, sheet_name="Ball In Court", index=False)
        (req_open if isinstance(req_open, pd.DataFrame) else pd.DataFrame()) \
            .to_excel(xl, sheet_name="Open Requests", index=False)
        clock.to_excel(xl, sheet_name="Deal Clock", index=False)
        write_blocks(xl, "Funnel", [
            (f"Stage funnel (independent stages; "
             f"{stages.attrs.get('inconsistencies', 0)} NDA/subdoc "
             "inconsistencies in source)", stages),
            ("Stuck cohort — SubDocs sent, not completed", stuck)])
        write_blocks(xl, "Latency Tail", [
            ("Distribution (mean deliberately omitted)", lat_sum),
            ("Weekly exceedance trend", lat_trend),
            ("Offending conversations (> 8 business hrs)", lat_off)])
        hyg.to_excel(xl, sheet_name="Hygiene", index=False)
        # raw data
        sig_df.to_excel(xl, sheet_name="Signals", index=False)
        contact_df.to_excel(xl, sheet_name="Contacts", index=False)
        opp_df.to_excel(xl, sheet_name="Opportunities", index=False)
        link_df.to_excel(xl, sheet_name="Contact-Opp Links", index=False)
        req_df.to_excel(xl, sheet_name="Info Requests", index=False)
        if not trk_df.empty:
            trk_df.to_excel(xl, sheet_name="Prospect Tracker", index=False)
        for ws in xl.book.worksheets:
            autofit(ws)
    print(f"wrote {out}")

    if WIN_DOWNLOADS.is_dir():
        dst = WIN_DOWNLOADS / out.name
        shutil.copy2(out, dst)
        print(f"copied to {dst}")


if __name__ == "__main__":
    main()
