"""RFI lifecycle: signal → Information Request promotion and the Answered
flip stamping completeddate (close-readiness directive §0.2).

Also pins add_business_days to the Sun–Thu work week — it silently used
Mon–Fri until 2026-08-02, drifting from bizhours.business_minutes.
"""
from datetime import datetime, timezone

from ingestion.classify import RfiResult
from ingestion.sync import SyncRun, add_business_days
from ingestion.tests.conftest import FakeDataverse, FakeGraph, ANNA, OPP1

BASE = 100000000


def ts(day, hour):
    return datetime(2026, 7, day, hour, tzinfo=timezone.utc)


def make_run(cfg, tmp_path, dv):
    return SyncRun(cfg, FakeGraph([]), dv, apply=True, state_dir=tmp_path,
                   codeversion="test")


def test_add_business_days_sun_thu():
    # Thursday +2 → Fri/Sat skipped → Sunday, Monday
    assert add_business_days(ts(30, 6), 2) == datetime(2026, 8, 3, 6,
                                                       tzinfo=timezone.utc)
    # Sunday is a workday in Israel: Sunday +1 → Monday
    assert add_business_days(ts(26, 6), 1) == ts(27, 6)


def test_create_rfi_writes_urgency_and_deadline(cfg, tmp_path):
    dv = FakeDataverse(apply=True)
    run = make_run(cfg, tmp_path, dv)
    rfi = RfiResult(is_info_request=True, category="Meeting",
                    description="Wants an intro call next week",
                    urgency="explicit_deadline", deadline="2026-08-10")
    msg = {"subject": "Intro?", "_ts": ts(30, 9), "_hash": "h" * 64}
    run._create_rfi({"new_engagementsignalid": "sig-0001"}, msg, rfi, ANNA, OPP1)
    req = next(iter(dv.requests.values()))
    assert req["new_name"] == "Wants an intro call next week"
    assert req["new_statedurgency"] == BASE + 2          # ExplicitDeadline
    assert req["new_explicitdeadline"] == "2026-08-10"
    assert req["new_duedate"].startswith("2026-08-10")   # deadline drives due
    assert req["_new_sourcesignal_value"] == "sig-0001"
    assert req["new_aigenerated"] and not req["new_humanconfirmed"]


def test_answered_flip_completes_request(cfg, tmp_path):
    dv = FakeDataverse(apply=True)
    conv = "CONV-1"
    dv.signals["sig-in"] = {
        "new_engagementsignalid": "sig-in", "new_conversationid": conv,
        "new_ismeaningful": True, "new_matchstatus": BASE,       # Confirmed
        "_new_contact_value": ANNA, "new_direction": BASE,       # Inbound
        "new_timestamputc": "2026-07-28T09:00:00Z",
        "new_responselatencymin": None, "new_rfistatus": BASE + 1}  # Open
    dv.signals["sig-out"] = {
        "new_engagementsignalid": "sig-out", "new_conversationid": conv,
        "new_ismeaningful": True, "new_matchstatus": BASE,
        "_new_contact_value": ANNA, "new_direction": BASE + 1,   # Outbound
        "new_timestamputc": "2026-07-29T10:00:00Z",
        "new_responselatencymin": None, "new_rfistatus": BASE}   # NA
    dv.requests["req-1"] = {
        "new_inforequestid": "req-1", "_new_sourcesignal_value": "sig-in",
        "new_status": BASE}                                      # New

    run = make_run(cfg, tmp_path, dv)
    run.touched_convs = {conv}
    run._latency_and_answered_pass()

    assert dv.signals["sig-in"]["new_rfistatus"] == BASE + 2     # Answered
    req = dv.requests["req-1"]
    assert req["new_status"] == BASE + 4                         # Completed
    assert req["new_completeddate"] == "2026-07-29T10:00:00Z"    # reply ts
