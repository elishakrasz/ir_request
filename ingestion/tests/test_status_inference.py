"""§4.5 status inference (claude_ir.md): system-set from thread traffic —
Outbound after last inbound → AwaitingInvestor; inbound after last outbound →
AwaitingInternal; no traffic for X days after our reply → PossiblyClosable.
Never the human's new_status, never auto-close.
"""
import dataclasses
import json
from datetime import datetime, timedelta, timezone

from ingestion.sync import SyncRun
from ingestion.tests.conftest import FakeDataverse, FakeGraph, ANNA

BASE = 100000000
IN, OUT = BASE, BASE + 1
CONV = "CONV-STATUS"
NOW = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def day(d, hour=9):
    return datetime(2026, 8, d, hour, tzinfo=timezone.utc)


def sig(dv, sid, direction, ts, meaningful=True):
    dv.signals[sid] = {"new_engagementsignalid": sid, "new_conversationid": CONV,
                       "new_ismeaningful": meaningful, "new_direction": direction,
                       "new_timestamputc": iso(ts), "_new_contact_value": ANNA}


def make_run(cfg, tmp_path, dv):
    return SyncRun(cfg, FakeGraph([]), dv, apply=True, state_dir=tmp_path,
                   codeversion="test")


def test_we_replied_last_is_awaiting_investor(cfg, tmp_path):
    dv = FakeDataverse()
    sig(dv, "in", IN, day(1))
    sig(dv, "out", OUT, day(2))
    label, prov = make_run(cfg, tmp_path, dv)._infer_status(CONV, now=day(5))
    assert label == "AwaitingInvestor"
    p = json.loads(prov)
    assert p["rule"] == "we replied after their last message"
    assert p["last_inbound"] == iso(day(1)) and p["last_outbound"] == iso(day(2))


def test_they_replied_last_is_awaiting_internal(cfg, tmp_path):
    dv = FakeDataverse()
    sig(dv, "out", OUT, day(1))
    sig(dv, "in", IN, day(2))
    label, prov = make_run(cfg, tmp_path, dv)._infer_status(CONV, now=day(5))
    assert label == "AwaitingInternal"
    assert json.loads(prov)["rule"] == "they replied after our last message"


def test_quiet_after_our_reply_is_possibly_closable_at_threshold(cfg, tmp_path):
    dv = FakeDataverse()
    sig(dv, "in", IN, day(1))
    sig(dv, "out", OUT, day(2))
    run = make_run(cfg, tmp_path, dv)                # cfg default: 14 days
    assert run._infer_status(CONV, now=day(2) + timedelta(days=13))[0] == "AwaitingInvestor"
    label, prov = run._infer_status(CONV, now=day(2) + timedelta(days=14))
    assert label == "PossiblyClosable"
    p = json.loads(prov)
    assert p["rule"] == "no traffic 14d after our last reply" and p["closable_days"] == 14


def test_closable_threshold_is_configurable(cfg, tmp_path):
    dv = FakeDataverse()
    sig(dv, "out", OUT, day(2))
    run = make_run(dataclasses.replace(cfg, status_closable_days=3), tmp_path, dv)
    assert run._infer_status(CONV, now=day(2) + timedelta(days=3))[0] == "PossiblyClosable"


def test_one_sided_threads(cfg, tmp_path):
    dv = FakeDataverse()
    sig(dv, "out", OUT, day(2))
    run = make_run(cfg, tmp_path, dv)
    assert run._infer_status(CONV, now=day(3))[0] == "AwaitingInvestor"
    dv2 = FakeDataverse()
    sig(dv2, "in", IN, day(2))
    assert make_run(cfg, tmp_path, dv2)._infer_status(CONV, now=day(3))[0] == \
        "AwaitingInternal"


def test_non_meaningful_traffic_is_ignored(cfg, tmp_path):
    """An auto-reply after their message must not read as 'we replied'."""
    dv = FakeDataverse()
    sig(dv, "in", IN, day(1))
    sig(dv, "ooo", OUT, day(2), meaningful=False)
    assert make_run(cfg, tmp_path, dv)._infer_status(CONV, now=day(3))[0] == \
        "AwaitingInternal"


def test_no_usable_conversation(cfg, tmp_path):
    run = make_run(cfg, tmp_path, FakeDataverse())
    assert run._infer_status(None) == (None, None)
    assert run._infer_status("CONV-EMPTY") == (None, None)


def test_status_fields_write_inferred_only(cfg, tmp_path):
    dv = FakeDataverse()
    now = datetime.now(timezone.utc)
    sig(dv, "in", IN, now - timedelta(days=2))
    sig(dv, "out", OUT, now - timedelta(days=1))
    fields = make_run(cfg, tmp_path, dv)._status_fields(CONV)
    assert fields["new_statusinferred"] == cfg.choices.statusinferred["AwaitingInvestor"]
    assert json.loads(fields["new_statusprovenance"])["closable_days"] == 14
    assert "new_status" not in fields                # status_actual is the human's


def test_status_fields_inert_without_column_or_traffic(cfg, tmp_path):
    dv = FakeDataverse()
    assert make_run(cfg, tmp_path, dv)._status_fields("CONV-EMPTY") == {}
    sig(dv, "in", IN, day(1))
    dv.has_attribute = lambda entity, attr: False
    assert make_run(cfg, tmp_path, dv)._status_fields(CONV) == {}
