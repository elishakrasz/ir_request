"""Investor Engagement Dashboard (spec Phase 4).

Run:  streamlit run dashboard/app.py
Reads Dataverse (DEV) via the shared app registration; ~5-min cache.
Writes: ONLY the two audited confirm-click actions (review queue, RFI status),
each stamped with the operator's name (modifiedbyhint).
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dashboard import data  # noqa: E402

# ── palette (validated reference instance — see dataviz skill) ────────────────
C_INBOUND = "#2a78d6"    # categorical slot 1 (blue)
C_OUTBOUND = "#008300"   # categorical slot 2 (green)
INK = "#0b0b0b"; INK2 = "#52514e"; MUTED = "#898781"
GRID = "#e1e0d9"; BASE = "#c3c2b7"
RAG = {"green": ("🟢", "Healthy"), "amber": ("🟡", "Cooling"), "red": ("🔴", "At risk")}

st.set_page_config(page_title="Investor Engagement", page_icon="📬", layout="wide")


# ── cached loads ──────────────────────────────────────────────────────────────
@st.cache_resource
def clients():
    cfg = data.get_cfg()
    return cfg, data.get_client(cfg)


@st.cache_data(ttl=300, show_spinner="Loading from Dataverse…")
def load_all():
    cfg, dv = clients()
    sigs = data.fetch_signals(dv, cfg)
    reqs = data.fetch_requests(dv, cfg)
    opps, contacts, links = data.fetch_scope(dv, cfg)
    return sigs, reqs, opps, contacts, links, pd.Timestamp.now(tz="UTC")


LOCAL_TZ = datetime.now().astimezone().tzinfo
STALE_MIN = 45          # cron is every 15 min; >2 missed ticks is worth saying


def freshness(ts, now):
    """'10:45 (2 min ago)' in the viewer's local time; ts is UTC."""
    if ts is None or pd.isna(ts):
        return "—", None
    mins = int((now - ts).total_seconds() // 60)
    if mins < 1:
        rel = "just now"
    elif mins < 60:
        rel = f"{mins} min ago"
    elif mins < 1440:
        rel = f"{mins // 60} h ago"
    else:
        rel = f"{mins // 1440} d ago"
    return f"{ts.tz_convert(LOCAL_TZ):%H:%M} ({rel})", mins


def plot_layout(fig, height=260):
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=28, b=8),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family='system-ui, "Segoe UI", sans-serif', color=INK2, size=12),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode="x unified")
    fig.update_xaxes(gridcolor=GRID, linecolor=BASE, tickfont=dict(color=MUTED))
    fig.update_yaxes(gridcolor=GRID, linecolor=BASE, tickfont=dict(color=MUTED),
                     rangemode="tozero")
    return fig


def kpis(sigs: pd.DataFrame, cohort_ids: set, reqs: pd.DataFrame, now):
    """All KPIs over meaningful, non-Excluded signals only (spec)."""
    s = sigs[sigs["meaningful"] & (sigs["matchstatus"] != "Excluded")]
    s30 = s[s["ts"] >= now - timedelta(days=30)]
    engaged = s30["contact_id"].nunique()
    pct = f"{100 * engaged / max(len(cohort_ids), 1):.0f}%"
    open_r = int(reqs["open"].sum()) if len(reqs) else 0
    over_r = int(reqs["overdue"].sum()) if len(reqs) else 0
    lat = s30[s30["direction"] == "Outbound"]["latency"].dropna()
    med = f"{lat.median() / 60:.1f} h" if len(lat) else "—"
    last = s["ts"].max()
    days = f"{(now - last).days} d" if pd.notna(last) else "—"
    return pct, f"{open_r} / {over_r}", med, days


def contact_health(sigs, reqs, roster: pd.DataFrame, now):
    s = sigs[sigs["meaningful"] & (sigs["matchstatus"] != "Excluded")]
    rows = []
    for _, c in roster.iterrows():
        mine = s[s["contact_id"] == c["contact_id"]]
        li = mine[mine["direction"] == "Inbound"]["ts"].max()
        lo = mine[mine["direction"] == "Outbound"]["ts"].max()
        last = max([t for t in (li, lo) if pd.notna(t)], default=pd.NaT)
        days = (now - last).days if pd.notna(last) else None
        n30 = len(mine[mine["ts"] >= now - timedelta(days=30)])
        my_reqs = reqs[reqs["contact_id"] == c["contact_id"]] if len(reqs) else reqs
        n_open = int(my_reqs["open"].sum()) if len(my_reqs) else 0
        n_over = int(my_reqs["overdue"].sum()) if len(my_reqs) else 0
        rag = ("red" if days is None or days > 21 or n_over
               else "amber" if days > 7 else "green")
        icon, label = RAG[rag]
        rows.append({
            "Contact": c["contact"], "Status": f"{icon} {label}",
            "Last inbound": li.date() if pd.notna(li) else None,
            "Last outbound": lo.date() if pd.notna(lo) else None,
            "Days since touch": days if days is not None else "never",
            "Msgs 30d": n30, "Open RFIs": n_open,
            "_sort": days if days is not None else 9999,
        })
    df = pd.DataFrame(rows)
    return (df.sort_values("_sort", ascending=False).drop(columns="_sort")
            if len(df) else df)


def timeline(sigs: pd.DataFrame, limit=60):
    arrow = {"Inbound": "⬅️", "Outbound": "➡️", "Internal": "↔️"}
    for _, r in sigs.head(limit).iterrows():
        flag = " 📨RFI" if r["rfistatus"] in ("Open", "Answered") else ""
        head = (f"{arrow.get(r['direction'], '')} {r['ts']:%Y-%m-%d %H:%M} — "
                f"**{r['subject'][:90]}**{flag}")
        with st.expander(head):
            st.caption(f"{r['sender']} · {r['contact']} · {r['opp'] or 'unassigned'} · "
                       f"match: {r['matchstatus']}/{r['matchmethod'] or '—'}")
            st.write(r["snippet"][:600] or "_no preview_")
            if r["sourcelink"]:
                st.caption(f"[open in mailbox (owner only)]({r['sourcelink']})")


def weekly_activity(sigs: pd.DataFrame):
    s = sigs[sigs["direction"].isin(["Inbound", "Outbound"])].copy()
    if not len(s):
        return None
    s["week"] = s["ts"].dt.to_period("W").dt.start_time
    piv = s.groupby(["week", "direction"]).size().unstack(fill_value=0)
    fig = go.Figure()
    for name, color in (("Inbound", C_INBOUND), ("Outbound", C_OUTBOUND)):
        if name in piv:
            fig.add_bar(x=piv.index, y=piv[name], name=name,
                        marker=dict(color=color, line=dict(width=0)))
    fig.update_layout(barmode="group", bargap=0.35, bargroupgap=0.15)
    return plot_layout(fig)


# ── load + sidebar ────────────────────────────────────────────────────────────
cfg, dv = clients()
sigs, reqs, opps, contacts, links, loaded_at = load_all()
now = pd.Timestamp.now(tz="UTC")

st.sidebar.title("📬 Investor Engagement")
page = st.sidebar.radio("View", ["Deal view", "Fund cohort", "Review queue",
                                 "Firm overview"])
who = st.sidebar.text_input("Your name (stamped on any action)",
                            value=st.session_state.get("who", ""))
st.session_state["who"] = who

funds = ["All funds"] + sorted(opps["fund"].unique()) if len(opps) else ["All funds"]
fund = st.sidebar.selectbox("Fund", funds)
d1, d2 = st.sidebar.date_input(
    "Date range", value=((now - timedelta(days=90)).date(), now.date()))
if st.sidebar.button("↻ Refresh data"):
    load_all.clear()
    st.rerun()

# Two different clocks: when this view pulled from Dataverse, and when the
# ingestion last ran. A fresh cache over a stalled sync is still stale data.
st.sidebar.caption(f"Loaded {freshness(loaded_at, now)[0]} · 5-min cache")
for label, ts in data.last_ingestion_runs().items():
    text, mins = freshness(ts, now)
    if mins is not None and mins > STALE_MIN:
        st.sidebar.warning(f"⚠️ {label} last ran {text}")
    else:
        st.sidebar.caption(f"{label} ran {text}")

fund_opps = opps if fund == "All funds" else opps[opps["fund"] == fund]
fund_links = links[links["opp_id"].isin(fund_opps["opp_id"])]
cohort_ids = set(fund_links["contact_id"])
in_range = sigs[(sigs["ts"].dt.date >= d1) & (sigs["ts"].dt.date <= d2)] \
    if len(sigs) else sigs

if not len(sigs):
    st.info("No engagement signals yet — run the ingestion apply first.")
    st.stop()

# ── pages ─────────────────────────────────────────────────────────────────────
if page == "Deal view":
    sel = st.selectbox("Opportunity", sorted(fund_opps["opp"].unique()))
    opp_id = fund_opps[fund_opps["opp"] == sel]["opp_id"].iloc[0]
    roster = contacts[contacts["contact_id"].isin(
        links[links["opp_id"] == opp_id]["contact_id"])]
    mine = in_range[in_range["opp_id"] == opp_id]
    # opp-level counts dedupe on messagekeyhash (1 msg × N contacts = 1 comm)
    mine_msgs = mine.drop_duplicates("keyhash")
    my_reqs = reqs[reqs["opp_id"] == opp_id] if len(reqs) else reqs

    alerts = []
    if len(my_reqs) and my_reqs["overdue"].sum():
        alerts.append(f"⚠️ {int(my_reqs['overdue'].sum())} overdue request(s)")
    silent = roster[~roster["contact_id"].isin(
        mine[mine["ts"] >= now - timedelta(days=21)]["contact_id"])]
    if len(silent):
        alerts.append(f"🔇 {len(silent)} contact(s) silent > 21d")
    if alerts:
        st.warning(" · ".join(alerts))

    pct, rfi, med, days = kpis(mine_msgs, set(roster["contact_id"]), my_reqs, now)
    for col, (label, val) in zip(st.columns(4), [
            ("Contacts engaged 30d", pct), ("Open / overdue RFIs", rfi),
            ("Median response 30d", med), ("Days since activity", days)]):
        col.metric(label, val)

    st.subheader("Contact health")
    st.dataframe(contact_health(mine, my_reqs, roster, now),
                 use_container_width=True, hide_index=True)

    st.subheader("Timeline")
    timeline(mine)

    st.subheader("Contact drill-down")
    pick = st.selectbox("Contact", ["—"] + sorted(roster["contact"].unique()))
    if pick != "—":
        cid = roster[roster["contact"] == pick]["contact_id"].iloc[0]
        cs = in_range[(in_range["contact_id"] == cid)
                      & in_range["latency"].notna()].sort_values("ts")
        if len(cs):
            fig = go.Figure(go.Scatter(
                x=cs["ts"], y=cs["latency"] / 60, mode="lines+markers",
                line=dict(color=C_INBOUND, width=2), marker=dict(size=8),
                name="response latency"))
            fig.update_yaxes(title_text="hours")
            st.plotly_chart(plot_layout(fig, 220), use_container_width=True)
        else:
            st.caption("No computed latencies in range for this contact.")
        timeline(in_range[in_range["contact_id"] == cid], limit=30)

elif page == "Fund cohort":
    # Explicit requirement: cohort-based, independent of per-email match status —
    # fund → opportunities → connected contacts → signals by contact + date.
    st.subheader(f"Fund cohort — {fund}")
    if fund == "All funds":
        st.info("Pick a specific fund in the sidebar.")
    else:
        cohort = in_range[in_range["contact_id"].isin(cohort_ids)]
        cohort_msgs = cohort.drop_duplicates("keyhash")
        roster = contacts[contacts["contact_id"].isin(cohort_ids)]
        pct, rfi, med, days = kpis(cohort_msgs, cohort_ids,
                                   reqs[reqs["contact_id"].isin(cohort_ids)]
                                   if len(reqs) else reqs, now)
        for col, (label, val) in zip(st.columns(4), [
                ("Cohort engaged 30d", pct),
                (f"Messages {d1}–{d2}", len(cohort_msgs)),
                ("Median response 30d", med), ("Days since activity", days)]):
            col.metric(label, str(val))
        fig = weekly_activity(cohort_msgs)
        if fig:
            st.plotly_chart(fig, use_container_width=True)
        st.subheader("Cohort contact health")
        st.dataframe(contact_health(cohort, reqs, roster, now),
                     use_container_width=True, hide_index=True)
        st.subheader("Cohort timeline")
        timeline(cohort)

elif page == "Review queue":
    q = in_range[in_range["matchstatus"].isin(["Suggested", "Unmatched"])]
    st.subheader(f"Review queue — {len(q)} signal(s) awaiting assignment")
    st.caption("Assigning teaches the matcher: later messages in the same "
               "conversation inherit the opportunity automatically.")
    opp_names = sorted(opps["opp"].unique())
    for _, r in q.head(100).iterrows():
        head = (f"{r['ts']:%Y-%m-%d} — {r['subject'][:80]} · {r['contact']}"
                + (f" · suggested: {r['opp']}" if r["opp"] else ""))
        with st.expander(head):
            st.caption(f"{r['sender']} · confidence {r['confidence']}")
            st.write(r["snippet"][:400] or "_no preview_")
            default = opp_names.index(r["opp"]) if r["opp"] in opp_names else 0
            choice = st.selectbox("Opportunity", opp_names, index=default,
                                  key=f"opp-{r['id']}")
            c1, c2 = st.columns(2)
            if c1.button("✓ Confirm", key=f"c-{r['id']}", disabled=not who):
                data.confirm_signal(
                    dv, cfg, r["id"],
                    opps[opps["opp"] == choice]["opp_id"].iloc[0], who)
                load_all.clear(); st.rerun()
            if c2.button("✕ Exclude", key=f"x-{r['id']}", disabled=not who):
                data.exclude_signal(dv, cfg, r["id"], who)
                load_all.clear(); st.rerun()
    if not who:
        st.info("Enter your name in the sidebar to enable actions.")

else:  # Firm overview
    st.subheader("Firm overview — worst first")
    rows = []
    for _, o in opps[opps["active"]].iterrows():
        mine = sigs[sigs["opp_id"] == o["opp_id"]]
        msgs = mine.drop_duplicates("keyhash")
        meaningful = msgs[msgs["meaningful"]]
        last = meaningful["ts"].max()
        my_reqs = reqs[reqs["opp_id"] == o["opp_id"]] if len(reqs) else reqs
        queue = len(mine[mine["matchstatus"].isin(["Suggested", "Unmatched"])])
        if not len(msgs) and not queue:
            continue
        rows.append({
            "Opportunity": o["opp"], "Fund": o["fund"],
            "Msgs 30d": len(meaningful[meaningful["ts"] >= now - timedelta(days=30)]),
            "Open RFIs": int(my_reqs["open"].sum()) if len(my_reqs) else 0,
            "Overdue": int(my_reqs["overdue"].sum()) if len(my_reqs) else 0,
            "Days since contact": (now - last).days if pd.notna(last) else None,
            "Review queue": queue,
        })
    df = pd.DataFrame(rows)
    if len(df):
        df = df.sort_values(["Overdue", "Days since contact", "Review queue"],
                            ascending=[False, False, False], na_position="first")
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No signals on actively-monitored opportunities yet.")
