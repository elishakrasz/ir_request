"""Close-readiness analysis views (§0.1, §1, §4, §5) — pure-function tests."""
from datetime import datetime, timezone

import pandas as pd

from ingestion import analysis

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def sig_row(**over):
    r = {"Timestamp (UTC)": "2026-07-30T09:00:00Z", "Direction": "Inbound",
         "Contact": "Anna LP", "Contact id": "c1", "Contact email": "anna@lp.com",
         "Opportunity": "Opp A", "Opportunity id": "o1", "Fund": "Fund X",
         "Sender": "anna@lp.com", "Subject": "Question", "Snippet": "hello",
         "Match status": "Confirmed", "Meaningful": True,
         "Response latency (biz min)": None, "Conversation id": "cv1",
         "Source link": "", "Message key hash": "m1"}
    r.update(over)
    return r


def test_mark_primary_collapses_within_opportunity():
    # same message attributed to two contacts on the SAME opportunity → one
    # primary; a third attribution on another opp keeps its own primary
    df = pd.DataFrame([
        sig_row(),
        sig_row(**{"Contact id": "c2", "Contact": "Bob", "Sender": "anna@lp.com",
                   "Contact email": "bob@x.com"}),
        sig_row(**{"Contact id": "c3", "Opportunity id": "o2",
                   "Contact email": "carol@y.com"}),
    ])
    out = analysis.mark_primary(df)
    assert out["Is Primary"].tolist() == [True, False, True]
    # sender-matching contact wins the primary slot
    assert out[out["Contact id"] == "c1"]["Is Primary"].all()


def test_ball_in_court_waiting_and_gap():
    df = pd.DataFrame([
        sig_row(**{"Direction": "Outbound", "Timestamp (UTC)":
                   "2026-07-15T09:00:00Z", "Message key hash": "m0"}),
        sig_row(),   # inbound 07-30 after our 07-15 outbound → our court
    ])
    df["Is Primary"] = True
    work, per = analysis.ball_in_court(df, NOW)
    assert len(work) == 1
    row = work.iloc[0]
    assert row["Contact"] == "Anna LP"
    # Thu 07-30 09:00 → Sun 08-02 12:00: Thu rest + Sun morning ≈ 1+ biz days
    assert 0.5 < row["Waiting (biz days)"] < 2
    assert row["Gap when they wrote (biz days)"] > 5   # 07-15 → 07-30
    assert row["Inbound subject"] == "Question"


def test_ball_not_ours_after_reply():
    df = pd.DataFrame([
        sig_row(),
        sig_row(**{"Direction": "Outbound", "Timestamp (UTC)":
                   "2026-07-31T09:00:00Z", "Message key hash": "m2"}),
    ])
    df["Is Primary"] = True
    work, per = analysis.ball_in_court(df, NOW)
    assert work.empty and not per["Ball in our court"].any()


def test_funnel_counts_and_stuck():
    trk = pd.DataFrame([
        {"code": "EXG-1", "name": "A", "email": "a@x.com", "committed": "$100",
         "ndaSigned": "y", "subdocsSent": "y", "commitIndicated": "y",
         "subdocsCompleted": "y"},
        {"code": "EXG-2", "name": "B", "email": "b@x.com", "committed": "—",
         "ndaSigned": "n", "subdocsSent": "y", "commitIndicated": "n",
         "subdocsCompleted": "n"},   # inconsistent (subdocs w/o NDA) + stuck
    ])
    stages, stuck = analysis.funnel(trk, pd.DataFrame(), pd.DataFrame())
    assert stages["Count"].tolist() == [2, 1, 2, 1, 1]
    assert stages.attrs["inconsistencies"] == 1
    assert len(stuck) == 1 and stuck.iloc[0]["Explanation"] == \
        "unexplained / needs outreach"


def test_latency_tail_no_mean():
    df = pd.DataFrame([
        sig_row(**{"Response latency (biz min)": 60.0}),
        sig_row(**{"Response latency (biz min)": 1000.0, "Message key hash": "m9",
                   "Contact id": "c9"}),
    ])
    df["Is Primary"] = True
    summ, trend, off = analysis.latency_tail(df)
    assert "Mean" not in "".join(summ.columns)
    assert summ[summ["Fund"] == "ALL FUNDS"]["Over 8h"].iloc[0] == 1
    assert len(off) == 1 and off.iloc[0]["Latency (biz hrs)"] == 16.7
