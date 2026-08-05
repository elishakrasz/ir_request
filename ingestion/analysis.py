"""IR close-readiness analysis layer (directive 2026-08-02).

Pure computation: takes the raw dataframes the export already pulls and
produces the six triage views — Ball-in-Court, Open Requests, Deal Clock,
Funnel, Latency Tail, Hygiene. No Dataverse writes. The only network use is
two cached, prescreened LLM checks (closed-lost detection; routing categories
come from the promotion sidecar).

Every tunable lives in THRESHOLDS — one config block, not scattered constants.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from . import llm
from .bizhours import business_minutes
from .config import STATE_DIR


@dataclass(frozen=True)
class Thresholds:
    request_default_bdays: int = 2          # §2: due = received + N biz days
    latency_bar_biz_min: int = 480          # §5: 8 business hours ≈ 1 biz day
    quiet_days: int = 30                    # §6: gone-quiet window
    stale_runway_fraction: float = 0.5      # §3: stale ≥ half remaining runway
    # §3 close dates by fund display name — fallback when the opportunity has
    # no estimatedclosedate. Update as deals are added.
    close_dates: dict = field(default_factory=lambda: {
        "Exigent SynthBee Holdings LP": "2026-08-15",
    })
    # §2.2 routing → typical owner (display-only; routing drives it)
    routing_owners: dict = field(default_factory=lambda: {
        "process_blocker": "IR ops (Dana/Eric)",
        "conviction": "Principal (Elie)",
        "deal_mechanics": "Principal/CFO",
        "scheduling": "Whoever is named",
    })


THRESHOLDS = Thresholds()

BOUNCE_SENDER = re.compile(r"postmaster@|mailer-daemon@|microsoftexchange", re.I)
BOUNCE_SUBJECT = re.compile(
    r"undeliverable|delivery (has )?failed|delivery status notification"
    r"|returned mail|couldn'?t be delivered|delivery failure", re.I)
# §6 closed-lost prescreen — deliberately broad; the LLM confirms each hit
DECLINE_RE = re.compile(
    r"\b(pass(ing)? on|we('| wi)ll pass|going to pass|decline|not (going to "
    r"|gonna )?invest|won'?t (be )?invest(ing)?|valuations? (of|at) this"
    r"|too (rich|high|expensive)|not a fit|not for us|sit(ting)? this one out"
    r"|no longer interested|decided not to)\b", re.I)

ROUTING_FILE = STATE_DIR / "request_routing.json"


# ── shared helpers ─────────────────────────────────────────────────────────
def _dt(v):
    """ISO string → aware datetime (UTC), or None."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def biz_days(start, end) -> float | None:
    """Business days (Sun–Thu, Asia/Jerusalem) between two timestamps."""
    a, b = _dt(start), _dt(end)
    if a is None or b is None:
        return None
    return round(business_minutes(a, b) / 1440, 1)


def _money(v) -> float:
    m = re.sub(r"[^0-9.]", "", str(v or ""))
    return float(m) if m else 0.0


def load_routing() -> dict:
    if ROUTING_FILE.exists():
        return json.loads(ROUTING_FILE.read_text())
    return {}


# ── §0.1 conversation-level dedup ──────────────────────────────────────────
def mark_primary(sig: pd.DataFrame) -> pd.DataFrame:
    """Adds `Is Primary`. A message (keyhash) legitimately appears once per
    contact; within one opportunity's scope the *n* contact-attributions of
    the same message collapse to a single primary row for volume/latency
    rollups. Primary = the attribution whose contact is on the message
    (sender match) first, then a stable id order."""
    sig = sig.copy()
    if sig.empty:
        sig["Is Primary"] = pd.Series(dtype=bool)
        return sig
    sender_match = [
        str(s or "").lower() in str(e or "").lower() or  # crude but stable:
        str(e or "").lower() in str(s or "").lower()     # email vs sender text
        for s, e in zip(sig["Sender"], sig["Contact email"])]
    sig["_pref"] = [0 if m else 1 for m in sender_match]
    # collapse within opportunity scope; rows with no opportunity stay unique
    # per contact (there is no opp rollup to inflate)
    grp = [f"o|{o}|{h}" if pd.notna(o) and o else f"c|{c}|{h}"
           for o, c, h in zip(sig["Opportunity id"], sig["Contact id"],
                              sig["Message key hash"])]
    grp = pd.Series(grp, index=sig.index)
    order = sig.assign(_g=grp).sort_values(["_pref", "Contact id"]) \
               .groupby("_g").cumcount()
    # groupby preserves original index alignment after the sort
    sig["Is Primary"] = order.reindex(sig.index).eq(0)
    return sig.drop(columns=["_pref"])


# ── §1 Ball-in-Court ───────────────────────────────────────────────────────
def ball_in_court(sig: pd.DataFrame, now: datetime):
    """Returns (worklist, per_contact). Meaningful confirmed signals only —
    per-contact rows are already unique per message, so no collapse needed."""
    s = sig[(sig["Meaningful"]) & (sig["Match status"] == "Confirmed")
            & sig["Contact id"].notna()]
    rows = []
    for cid, g in s.groupby("Contact id"):
        inb = g[g["Direction"] == "Inbound"]
        out = g[g["Direction"] == "Outbound"]
        last_in = inb["Timestamp (UTC)"].max() if len(inb) else None
        last_out = out["Timestamp (UTC)"].max() if len(out) else None
        ours = last_in is not None and (last_out is None or last_in > last_out)
        trigger = inb[inb["Timestamp (UTC)"] == last_in].iloc[0] if ours else None
        rows.append({
            "Contact id": cid,
            "Contact": g["Contact"].iloc[0],
            "Email": g["Contact email"].iloc[0],
            "Fund": (trigger if ours else g.iloc[0])["Fund"],
            "Opportunity": (trigger if ours else g.iloc[0])["Opportunity"],
            "Ball in our court": ours,
            "Waiting (biz days)": biz_days(last_in, now) if ours else None,
            "Their inbound at": last_in,
            "Our last outbound at": last_out,
            "Gap when they wrote (biz days)":
                biz_days(last_out, last_in) if ours and last_out else None,
            "Inbound subject": trigger["Subject"] if ours else "",
            "Inbound snippet": (trigger["Snippet"] or "")[:200] if ours else "",
            "Open in Outlook": trigger["Source link"] if ours else "",
        })
    per_contact = pd.DataFrame(rows)
    if per_contact.empty:
        return per_contact, per_contact
    work = per_contact[per_contact["Ball in our court"]] \
        .sort_values("Waiting (biz days)", ascending=False) \
        .drop(columns=["Ball in our court"])
    return work, per_contact


# ── §2 Open Requests with aging ────────────────────────────────────────────
def open_requests(req: pd.DataFrame, now: datetime, th=THRESHOLDS):
    if req.empty:
        return req
    routing = load_routing()
    r = req.copy()
    # stored column first (PROD since 2026-08-03), sidecar fallback
    fallback = r["Source signal id"].map(routing)
    if "Stored routing" in r.columns:
        r["Routing"] = r["Stored routing"].combine_first(fallback)
    else:
        r["Routing"] = fallback
    r["Routing"] = r["Routing"].fillna("(unclassified)")
    r["Suggested owner"] = r["Routing"].map(th.routing_owners).fillna("")
    r["Age (biz days)"] = [biz_days(v, now) for v in r["Received"]]
    open_mask = ~r["Status"].isin(["Completed", "Cancelled"])
    due = [_dt(v) for v in r["Due"]]
    r["Overdue"] = [bool(o and d and now > d) for o, d in zip(open_mask, due)]
    out = r[open_mask].sort_values(["Overdue", "Age (biz days)"],
                                   ascending=[False, False])
    cols = ["Received", "Age (biz days)", "Due", "Overdue", "Routing",
            "Suggested owner", "Title", "Contact", "Opportunity", "Status",
            "Stated urgency", "Explicit deadline", "Category"]
    return out[[c for c in cols if c in out.columns]]


# ── §3 Deal-Clock staleness ────────────────────────────────────────────────
def deal_clock(trk: pd.DataFrame, per_contact_sig: pd.DataFrame,
               now: datetime, fund: str = "Exigent SynthBee Holdings LP",
               th=THRESHOLDS):
    """Tracker prospects against the fund's close date. days_stale = days
    since the LATER of last status change / last meaningful signal."""
    if trk.empty or fund not in th.close_dates:
        return pd.DataFrame()
    close = _dt(th.close_dates[fund])
    days_to_close = max((close - now).days, 0)
    last_sig = {}
    if not per_contact_sig.empty:
        for _, row in per_contact_sig.iterrows():
            cand = [d for d in map(_dt, (row["Their inbound at"],
                                         row["Our last outbound at"])) if d]
            if cand and isinstance(row["Email"], str) and row["Email"]:
                last_sig[row["Email"].lower()] = max(cand)
    rows = []
    for _, t in trk.iterrows():
        if str(t.get("subdocsCompleted")) in ("y", "True", "true"):
            continue                       # already closed — no clock
        status = str(t.get("status") or "").strip()
        if status in ("", "—", "Archived"):
            continue    # not in motion — §6 covers never-worked prospects
        anchors = [x for x in (_dt(t.get("statusSince")),
                               _dt(last_sig.get(str(t.get("email", "")).lower())))
                   if x]
        stale = (now - max(anchors)).days if anchors else None
        ratio = (round(stale / days_to_close, 2)
                 if stale is not None and days_to_close else None)
        rows.append({
            "Code": t.get("code"), "Prospect": t.get("name"),
            "Email": t.get("email"), "Status": t.get("status"),
            "Status since": t.get("statusSince"),
            "Days stale": stale, "Days to close": days_to_close,
            "Stale / runway": ratio,
            "At risk": bool(stale is not None and days_to_close
                            and stale >= days_to_close * th.stale_runway_fraction),
            "Pending action": t.get("action", ""),
        })
    df = pd.DataFrame(rows)
    return df.sort_values("Stale / runway", ascending=False, na_position="last") \
        if len(df) else df


# ── §4 Funnel conversion ───────────────────────────────────────────────────
FUNNEL_STAGES = [("Prospects", None), ("NDA signed", "ndaSigned"),
                 ("SubDocs sent", "subdocsSent"),
                 ("Commit indicated", "commitIndicated"),
                 ("SubDocs completed", "subdocsCompleted")]


def _yes(v) -> bool:
    return str(v).strip().lower() in ("y", "yes", "true")


def funnel(trk: pd.DataFrame, per_contact: pd.DataFrame,
           open_req: pd.DataFrame):
    """Returns (stages, stuck). Stages are independent booleans — no forced
    monotonicity; inconsistencies are counted, not hidden."""
    if trk.empty:
        return pd.DataFrame(), pd.DataFrame()
    counts, dollars = [], []
    for label, col in FUNNEL_STAGES:
        cohort = trk if col is None else trk[trk[col].map(_yes)]
        counts.append(len(cohort))
        dollars.append(sum(_money(v) for v in cohort.get("committed", [])))
    stages = pd.DataFrame({
        "Stage": [s for s, _ in FUNNEL_STAGES],
        "Count": counts,
        "Committed $": [round(d) for d in dollars],
        "Conversion from prior": ["—"] + [
            f"{counts[i] / counts[i - 1]:.0%}" if counts[i - 1] else "—"
            for i in range(1, len(counts))],
    })
    # the operational gap: SubDocs sent but not completed (directive §4)
    sent_i = [s for s, _ in FUNNEL_STAGES].index("SubDocs sent")
    n_stuck = int((trk["subdocsSent"].map(_yes)
                   & ~trk["subdocsCompleted"].map(_yes)).sum())
    stages["Widest gap"] = [f"◀ {n_stuck} sent, not completed"
                            if i == sent_i else ""
                            for i in range(len(counts))]

    # stuck cohort: subdocs sent, not completed — one explanation each
    stuck_rows = trk[trk["subdocsSent"].map(_yes)
                     & ~trk["subdocsCompleted"].map(_yes)]
    blockers = set()
    if not open_req.empty:
        blockers = {c for c, r in zip(open_req["Contact"], open_req["Routing"])
                    if r == "process_blocker"}
    our_court = set()
    if not per_contact.empty:
        pc = per_contact[per_contact["Ball in our court"]]
        our_court = {str(e).lower() for e in pc["Email"]} \
            | {str(n).lower() for n in pc["Contact"]}
    stuck = []
    for _, t in stuck_rows.iterrows():
        name, email = t.get("name", ""), str(t.get("email", "")).lower()
        if name in blockers:
            why = "open process_blocker request"
        elif email in our_court or str(name).lower() in our_court:
            why = "awaiting our reply"
        else:
            why = "unexplained / needs outreach"
        stuck.append({"Code": t.get("code"), "Prospect": name,
                      "Email": t.get("email"), "Status": t.get("status"),
                      "Committed $": round(_money(t.get("committed"))),
                      "Explanation": why})
    stuck_df = pd.DataFrame(stuck).sort_values("Explanation") \
        if stuck else pd.DataFrame()
    n_incons = int((trk["subdocsSent"].map(_yes)
                    & ~trk["ndaSigned"].map(_yes)).sum())
    stages.attrs["inconsistencies"] = n_incons
    return stages, stuck_df


# ── §5 Latency tail ────────────────────────────────────────────────────────
def latency_tail(sig: pd.DataFrame, th=THRESHOLDS):
    """Median / p90 / exceedance count — the mean is deliberately absent."""
    s = sig[(sig["Is Primary"]) & (sig["Meaningful"])
            & (sig["Match status"] == "Confirmed")
            & sig["Response latency (biz min)"].notna()]
    if s.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    summary = []
    for fund, g in [("ALL FUNDS", s)] + list(s.groupby("Fund")):
        lat = g["Response latency (biz min)"]
        summary.append({
            "Fund": fund, "Replies paired": len(lat),
            "Median (biz hrs)": round(lat.median() / 60, 1),
            "p90 (biz hrs)": round(lat.quantile(0.9) / 60, 1),
            "Max (biz hrs)": round(lat.max() / 60, 1),
            f"Over {th.latency_bar_biz_min // 60}h":
                int((lat > th.latency_bar_biz_min).sum()),
        })
    off = s[s["Response latency (biz min)"] > th.latency_bar_biz_min].copy()
    off["Latency (biz hrs)"] = (off["Response latency (biz min)"] / 60).round(1)
    offenders = off.sort_values("Latency (biz hrs)", ascending=False)[
        ["Fund", "Contact", "Timestamp (UTC)", "Subject", "Latency (biz hrs)",
         "Conversation id", "Source link"]]
    wk = off.assign(Week=pd.to_datetime(off["Timestamp (UTC)"], utc=True,
                                        format="ISO8601")
                    .dt.to_period("W").astype(str)) \
        .groupby("Week").size().reset_index(name="Exceedances").tail(8)
    return pd.DataFrame(summary), wk, offenders


# ── §6 Coverage & hygiene ──────────────────────────────────────────────────
def hygiene(sig_all: pd.DataFrame, contacts: pd.DataFrame,
            links: pd.DataFrame, opps: pd.DataFrame, now: datetime,
            th=THRESHOLDS, use_llm: bool = True):
    """One row per (contact, flag): never_contacted / gone_quiet /
    delivery_failure / suggested_closed_lost."""
    fund_of = dict(zip(opps["Opportunity id"], opps["Fund"]))
    contact_funds = {}
    for _, l in links.iterrows():
        f = fund_of.get(l["Opportunity id"])
        contact_funds.setdefault(l["Contact id"], set()).add(
            f if isinstance(f, str) and f else "(no fund)")
    meaningful = sig_all[sig_all["Meaningful"]
                         & (sig_all["Match status"] != "Excluded")]
    last_touch = meaningful.groupby("Contact id")["Timestamp (UTC)"].max()
    out = []

    def funds(cid):
        return " · ".join(sorted(contact_funds.get(cid, {"(no fund)"})))

    by_id = contacts.set_index("Contact id") if not contacts.empty else contacts
    for cid in contact_funds:
        if cid not in by_id.index:
            continue
        c = by_id.loc[cid]
        lt = last_touch.get(cid)
        if lt is None:
            out.append({"Flag": "never_contacted", "Contact": c["Contact"],
                        "Email": c["Email"], "Fund": funds(cid),
                        "Evidence": "no meaningful signal on record",
                        "Last touch": None})
        elif (now - _dt(lt)).days > th.quiet_days:
            out.append({"Flag": "gone_quiet", "Contact": c["Contact"],
                        "Email": c["Email"], "Fund": funds(cid),
                        "Evidence": f"last meaningful signal "
                                    f"{(now - _dt(lt)).days} days ago",
                        "Last touch": lt})

    # delivery failures — scan ALL signals (bounces are non-meaningful)
    bounce = sig_all[[bool(BOUNCE_SENDER.search(str(s)))
                      or bool(BOUNCE_SUBJECT.search(str(t)))
                      for s, t in zip(sig_all["Sender"], sig_all["Subject"])]]
    for _, b in bounce.iterrows():
        cid = b["Contact id"]
        if not cid or cid not in by_id.index:
            continue
        later_out = meaningful[(meaningful["Contact id"] == cid)
                               & (meaningful["Direction"] == "Outbound")
                               & (meaningful["Timestamp (UTC)"]
                                  > b["Timestamp (UTC)"])]
        if later_out.empty:
            c = by_id.loc[cid]
            out.append({"Flag": "delivery_failure", "Contact": c["Contact"],
                        "Email": c["Email"], "Fund": funds(cid),
                        "Evidence": f"bounce {str(b['Timestamp (UTC)'])[:10]}: "
                                    f"{b['Subject'][:80]} — no outbound since",
                        "Last touch": b["Timestamp (UTC)"]})

    # suggested closed-lost — prescreen last inbound, LLM confirms (cached)
    inbound = meaningful[(meaningful["Direction"] == "Inbound")
                         & (meaningful["Match status"] == "Confirmed")]
    if not inbound.empty:
        idx = inbound.groupby("Contact id")["Timestamp (UTC)"].idxmax()
        for _, m in inbound.loc[idx].iterrows():
            text = f"{m['Subject']} {m['Snippet']}"
            if not DECLINE_RE.search(text):
                continue
            verdict = (llm.classify_closed_lost(m["Subject"], m["Snippet"] or "")
                       if use_llm else {"declined": True, "reason": "keyword"})
            if verdict and verdict.get("declined") and m["Contact id"] in by_id.index:
                c = by_id.loc[m["Contact id"]]
                out.append({"Flag": "suggested_closed_lost",
                            "Contact": c["Contact"], "Email": c["Email"],
                            "Fund": funds(m["Contact id"]),
                            "Evidence": (verdict.get("reason") or "")[:200],
                            "Last touch": m["Timestamp (UTC)"]})

    order = {"delivery_failure": 0, "suggested_closed_lost": 1,
             "never_contacted": 2, "gone_quiet": 3}
    df = pd.DataFrame(out)
    return df.sort_values(["Flag", "Contact"],
                          key=lambda s: s.map(order) if s.name == "Flag" else s) \
        if len(df) else df
