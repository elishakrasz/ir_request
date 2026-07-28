"""Dataverse data layer for the Streamlit dashboard.

Read-mostly: signals/requests/opportunities/contacts into pandas. The ONLY
writes are the two audited confirm-click actions (spec Phase 4): updating an
Information Request's status/owner and confirming/excluding a signal's
opportunity — every write stamps modifiedbyhint with the human's identity.
"""
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ingestion.config import Config           # noqa: E402
from ingestion.dataverse_client import DataverseClient  # noqa: E402

FMT = "@OData.Community.Display.V1.FormattedValue"


def get_cfg() -> Config:
    return Config.from_env()


def get_client(cfg: Config) -> DataverseClient:
    return DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                           cfg.client_secret, cfg.prefix, apply=True)


def rev(d: dict) -> dict:
    return {v: k for k, v in d.items()}


def fetch_signals(dv: DataverseClient, cfg: Config) -> pd.DataFrame:
    p, ch = cfg.prefix, cfg.choices
    rows = dv.query(
        f"{p}engagementsignals?$select={p}name,{p}direction,{p}channel,"
        f"{p}timestamputc,{p}sender,{p}snippet,{p}conversationid,{p}rfistatus,"
        f"{p}matchstatus,{p}matchmethod,{p}matchconfidence,{p}responselatencymin,"
        f"{p}ismeaningful,{p}sourcelink,{p}messagekeyhash")
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([{
        "id": r[f"{p}engagementsignalid"],
        "subject": r.get(f"{p}name") or "",
        "ts": r.get(f"{p}timestamputc"),
        "direction": rev(ch.direction).get(r.get(f"{p}direction"), "?"),
        "sender": r.get(f"{p}sender") or "",
        "snippet": r.get(f"{p}snippet") or "",
        "conv": r.get(f"{p}conversationid") or "",
        "rfistatus": rev(ch.rfistatus).get(r.get(f"{p}rfistatus"), "NA"),
        "matchstatus": rev(ch.matchstatus).get(r.get(f"{p}matchstatus"), "?"),
        "matchmethod": rev(ch.matchmethod).get(r.get(f"{p}matchmethod"), ""),
        "confidence": r.get(f"{p}matchconfidence"),
        "latency": r.get(f"{p}responselatencymin"),
        "meaningful": bool(r.get(f"{p}ismeaningful")),
        "sourcelink": r.get(f"{p}sourcelink") or "",
        "keyhash": r.get(f"{p}messagekeyhash") or "",
        "contact_id": r.get(f"_{p}contact_value"),
        "contact": r.get(f"_{p}contact_value{FMT}") or "(unknown)",
        "opp_id": r.get(f"_{p}opportunity_value"),
        "opp": r.get(f"_{p}opportunity_value{FMT}") or "",
    } for r in rows])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values("ts", ascending=False)


def fetch_requests(dv: DataverseClient, cfg: Config) -> pd.DataFrame:
    p, ch = cfg.prefix, cfg.choices
    rows = dv.query(
        f"{p}inforequests?$select={p}name,{p}status,{p}category,{p}receiveddate,"
        f"{p}duedate,{p}completeddate,{p}aigenerated,{p}humanconfirmed")
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([{
        "id": r[f"{p}inforequestid"],
        "title": r.get(f"{p}name") or "",
        "status": rev(ch.req_status).get(r.get(f"{p}status"), "?"),
        "category": rev(ch.req_category).get(r.get(f"{p}category"), ""),
        "received": r.get(f"{p}receiveddate"),
        "due": r.get(f"{p}duedate"),
        "contact_id": r.get(f"_{p}contact_value"),
        "contact": r.get(f"_{p}contact_value{FMT}") or "",
        "opp_id": r.get(f"_{p}opportunity_value"),
    } for r in rows])
    for col in ("received", "due"):
        df[col] = pd.to_datetime(df[col], utc=True)
    open_mask = ~df["status"].isin(["Completed", "Cancelled"])
    now = pd.Timestamp.now(tz="UTC")
    df["overdue"] = open_mask & df["due"].notna() & (df["due"] < now)  # derived
    df["open"] = open_mask
    return df


def fetch_scope(dv: DataverseClient, cfg: Config):
    """opps df (with fund), contact roster, and contact↔opp links."""
    opp_rows = dv.fetch_opportunities(cfg.fund_lookup)
    fund_key = f"_{cfg.fund_lookup}_value"
    opps = pd.DataFrame([{
        "opp_id": o["opportunityid"],
        "opp": o.get("name") or "",
        "fund_id": o.get(fund_key),
        "fund": o.get(fund_key + FMT) or "(no fund)",
        "active": bool(o.get(f"{cfg.prefix}activemonitoring")),
        "parent_contact": o.get("_parentcontactid_value"),
    } for o in opp_rows])

    links = []   # contact_id ↔ opp_id (parent contact + connections)
    for o in opp_rows:
        if o.get("_parentcontactid_value"):
            links.append((o["_parentcontactid_value"], o["opportunityid"]))
    for c in dv.fetch_connections():
        if c.get("record1objecttypecode") == 2:
            links.append((c.get("_record1id_value"), c.get("_record2id_value")))
        else:
            links.append((c.get("_record2id_value"), c.get("_record1id_value")))
    link_df = pd.DataFrame(links, columns=["contact_id", "opp_id"]).dropna()
    link_df = link_df[link_df["opp_id"].isin(opps["opp_id"])].drop_duplicates()

    contact_rows = dv.fetch_contacts(sorted(link_df["contact_id"].unique()))
    contacts = pd.DataFrame([{
        "contact_id": c["contactid"],
        "contact": c.get("fullname") or "(no name)",
        "email": c.get("emailaddress1") or "",
    } for c in contact_rows])
    return opps, contacts, link_df


# ── audited write actions ─────────────────────────────────────────────────────
def confirm_signal(dv: DataverseClient, cfg: Config, signal_id: str,
                   opp_id: str, who: str):
    p, ch = cfg.prefix, cfg.choices
    dv.patch(f"{p}engagementsignals", signal_id, {
        f"{p}matchstatus": ch.matchstatus["Confirmed"],
        f"{p}matchmethod": ch.matchmethod["Manual"],
        f"{p}matchconfidence": 100,
        f"{p}opportunity@odata.bind": f"/opportunities({opp_id})",
        f"{p}modifiedbyhint": who[:200],
    }, f"manual confirm {signal_id[:8]} by {who}")


def exclude_signal(dv: DataverseClient, cfg: Config, signal_id: str, who: str):
    p, ch = cfg.prefix, cfg.choices
    dv.patch(f"{p}engagementsignals", signal_id, {
        f"{p}matchstatus": ch.matchstatus["Excluded"],
        f"{p}modifiedbyhint": who[:200],
    }, f"manual exclude {signal_id[:8]} by {who}")


def update_request_status(dv: DataverseClient, cfg: Config, request_id: str,
                          status: str, who: str):
    p, ch = cfg.prefix, cfg.choices
    payload = {f"{p}status": ch.req_status[status], f"{p}modifiedbyhint": who[:200]}
    if status == "Completed":
        payload[f"{p}completeddate"] = pd.Timestamp.now(tz="UTC").strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    dv.patch(f"{p}inforequests", request_id, payload,
             f"request {request_id[:8]} -> {status} by {who}")
