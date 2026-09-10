"""Classifier-gated request merge (2026-09-10).

Every follow-up email used to open its own ticket: Victoria Coates had 6 open
tickets from one Aug-6 scheduling thread, Charles Lazar 4 for arranging one
call. A follow-up on a thread that already carries an open ticket now attaches
to it — but only when the classifier agrees it is the same ask, because a
genuinely new request buried inside an old ticket is lost work.
"""
from datetime import datetime, timezone

import pytest

from ingestion.classify import RfiResult
from ingestion.sync import SyncRun
from ingestion.tests.conftest import FakeDataverse, FakeGraph, ANNA, OPP1

BASE = 100000000
NEW, INPROGRESS, WAITING_EXT = BASE, BASE + 1, BASE + 3
CONV = "CONV-SCHEDULING-1"


def make_run(cfg, tmp_path, dv):
    cfg.create_requests = True
    cfg.intake_mailboxes = []
    return SyncRun(cfg, FakeGraph([]), dv, apply=True, state_dir=tmp_path,
                   codeversion="test")


def seed_open_ticket(dv, status=NEW, title="Sender wants to schedule a call."):
    dv.signals["sig-open"] = {
        "new_engagementsignalid": "sig-open", "new_conversationid": CONV,
        "new_ismeaningful": True, "new_matchstatus": BASE,
        "_new_contact_value": ANNA, "new_direction": BASE,
        "new_timestamputc": "2026-09-06T14:23:00Z", "new_rfistatus": BASE + 1,
        "new_responselatencymin": None}
    dv.requests["req-open"] = {
        "new_inforequestid": "req-open", "_new_sourcesignal_value": "sig-open",
        "new_status": status, "new_name": title,
        "new_receiveddate": "2026-09-06T14:23:00Z"}


def follow_up(hash_char="f"):
    return {
        "id": "graph-2", "subject": "Re: Reconnecting",
        "bodyPreview": "Can we move the call to 5:30pm instead?",
        "conversationId": CONV, "internetMessageId": "<m2@x>",
        "from": {"emailAddress": {"address": "anna.lp@lpfund.com", "name": "Anna"}},
        "webLink": "https://outlook/x",
        "_ts": datetime(2026, 9, 7, 9, 3, tzinfo=timezone.utc),
        "_sender": "anna.lp@lpfund.com", "_recipients": ["ir@exigentcap.com"],
        "_participants": ["anna.lp@lpfund.com", "ir@exigentcap.com"],
        "_hash": hash_char * 64, "_mailbox": "ir@exigentcap.com",
        "_folder": "inbox", "_contacts": {ANNA: {OPP1}}, "_intake": False,
        "_noise_reason": None,
    }


@pytest.fixture(autouse=True)
def _is_a_request(monkeypatch):
    monkeypatch.setattr("ingestion.sync.classify", lambda subject, text:
                        RfiResult(is_info_request=True, category="Meeting",
                                  description="Asks to move the call to 5:30pm"))


def verdict(monkeypatch, same, confidence=95, reason="same call"):
    monkeypatch.setattr("ingestion.sync.llm.same_request",
                        lambda *a, **k: {"same_request": same,
                                         "confidence": confidence,
                                         "reason": reason})


def test_follow_up_merges_into_the_open_ticket(cfg, tmp_path, monkeypatch):
    dv = FakeDataverse(apply=True); seed_open_ticket(dv)
    verdict(monkeypatch, True)
    run = make_run(cfg, tmp_path, dv)
    run._handle(follow_up(), {}, {}, {}, {})
    assert len(dv.requests) == 1                    # no second ticket
    assert run.counts.get("rfi_merged") == 1
    assert "merge|+msg:" in dv.requests["req-open"]["new_modifiedbyhint"]


def test_a_different_ask_on_the_same_thread_gets_its_own_ticket(cfg, tmp_path,
                                                                monkeypatch):
    """The whole point of gating: a new ask must not be swallowed."""
    dv = FakeDataverse(apply=True); seed_open_ticket(dv)
    verdict(monkeypatch, False, reason="asks for a different document")
    run = make_run(cfg, tmp_path, dv)
    run._handle(follow_up(), {}, {}, {}, {})
    assert len(dv.requests) == 2
    assert "rfi_merged" not in run.counts


def test_low_confidence_yes_falls_through_to_a_new_ticket(cfg, tmp_path,
                                                          monkeypatch):
    dv = FakeDataverse(apply=True); seed_open_ticket(dv)
    verdict(monkeypatch, True, confidence=40)
    run = make_run(cfg, tmp_path, dv)
    run._handle(follow_up(), {}, {}, {}, {})
    assert len(dv.requests) == 2


def test_classifier_unavailable_falls_through(cfg, tmp_path, monkeypatch):
    dv = FakeDataverse(apply=True); seed_open_ticket(dv)
    monkeypatch.setattr("ingestion.sync.llm.same_request", lambda *a, **k: None)
    run = make_run(cfg, tmp_path, dv)
    run._handle(follow_up(), {}, {}, {}, {})
    assert len(dv.requests) == 2


def test_no_open_ticket_on_the_thread_creates_one(cfg, tmp_path, monkeypatch):
    dv = FakeDataverse(apply=True)
    verdict(monkeypatch, True)
    run = make_run(cfg, tmp_path, dv)
    run._handle(follow_up(), {}, {}, {}, {})
    assert len(dv.requests) == 1


def test_merge_revives_a_ticket_parked_on_the_investor(cfg, tmp_path, monkeypatch):
    """WaitingExternal means we are waiting on them — they just wrote."""
    dv = FakeDataverse(apply=True); seed_open_ticket(dv, status=WAITING_EXT)
    verdict(monkeypatch, True)
    run = make_run(cfg, tmp_path, dv)
    run._handle(follow_up(), {}, {}, {}, {})
    assert dv.requests["req-open"]["new_status"] == NEW


def test_merge_can_be_switched_off(cfg, tmp_path, monkeypatch):
    dv = FakeDataverse(apply=True); seed_open_ticket(dv)
    verdict(monkeypatch, True)
    cfg.merge_requests = False
    run = make_run(cfg, tmp_path, dv)
    run._handle(follow_up(), {}, {}, {}, {})
    assert len(dv.requests) == 2
