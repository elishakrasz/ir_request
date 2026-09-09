"""A weak OPPORTUNITY match must not bury a real REQUEST (the Phil Rosen case,
2026-09-08): inbound mail from a known contact that the classifier read as a
request scored 30 on the opportunity matcher, fell under review_min=50, and was
written Excluded/low_confidence — no Information Request, invisible to the
dashboard. The intake rescue could not save it: the IR-request route runs with
intake_mailboxes=[], so _intake is never set for ir@.

Guardrails pinned here: rescue only for INBOUND mail (requests come from
replies/incoming only) that is associated with a contact.
"""
from datetime import datetime, timezone

import pytest

from ingestion.classify import RfiResult
from ingestion.sync import SyncRun
from ingestion.tests.conftest import FakeDataverse, FakeGraph, ANNA, OPP1

BASE = 100000000
CONFIRMED, SUGGESTED, UNMATCHED, EXCLUDED = BASE, BASE + 1, BASE + 2, BASE + 3
OPP2 = "11111111-0000-0000-0000-000000000009"

# Two live opps on the contact → matcher returns (top candidate, None, 30):
# below review_min, which is exactly the score that buried the real message.
OPP_META = {OPP1: {"live": True, "aliases": [], "prospectcode": "EXG-1040"},
            OPP2: {"live": True, "aliases": [], "prospectcode": "EXG-2050"}}


def make_msg(direction_out=False, contacts=None):
    sender = ("ir@exigentcap.com" if direction_out else "jprosen@aol.com")
    recips = (["jprosen@aol.com"] if direction_out else ["ir@exigentcap.com"])
    return {
        "id": "graph-id-rosen",
        "subject": "Re: Exigent's New Fund Administrator - Carta",
        "bodyPreview": "Please have these added to my son's existing Carta portal",
        "conversationId": "CONV-CARTA-1",
        "internetMessageId": "<971918694@mail.yahoo.com>",
        "from": {"emailAddress": {"address": sender, "name": "Phil Rosen"}},
        "webLink": "https://outlook/x",
        "_ts": datetime(2026, 9, 8, 17, 4, tzinfo=timezone.utc),
        "_sender": sender,
        "_recipients": recips,
        "_participants": [sender] + recips,
        "_hash": "b" * 64,
        "_mailbox": "ir@exigentcap.com",
        "_folder": "inbox" if not direction_out else "sentitems",
        "_contacts": {ANNA: {OPP1, OPP2}} if contacts is None else contacts,
        "_intake": False,          # the IR-request route: intake_mailboxes=[]
        "_noise_reason": None,
    }


def make_run(cfg, tmp_path, dv):
    cfg.intake_mailboxes = []      # as run_ir_request.py configures it
    cfg.create_requests = True     # only this route opens tickets
    return SyncRun(cfg, FakeGraph([]), dv, apply=True, state_dir=tmp_path,
                   codeversion="test")


@pytest.fixture
def asks(monkeypatch):
    """Classifier reads the message as a genuine request."""
    monkeypatch.setattr("ingestion.sync.classify", lambda subject, text:
                        RfiResult(is_info_request=True, category="AccountAdmin",
                                  description="Add his son to the Carta portal",
                                  confidence=90))


@pytest.fixture
def not_a_request(monkeypatch):
    monkeypatch.setattr("ingestion.sync.classify",
                        lambda subject, text: RfiResult(is_info_request=False))


def test_low_confidence_request_reaches_review_queue(cfg, tmp_path, asks):
    dv = FakeDataverse(apply=True)
    run = make_run(cfg, tmp_path, dv)
    run._handle(make_msg(), OPP_META, {}, {}, {})

    sig = next(iter(dv.signals.values()))
    assert sig["new_matchstatus"] == SUGGESTED      # was Excluded
    assert sig.get("new_noisereason") is None       # was "low_confidence"
    assert sig["new_ismeaningful"] is True          # counts in KPIs again
    assert sig["new_matchconfidence"] == 30         # the score itself is honest
    assert run.counts["rfi_rescued"] == 1
    # and the ticket the team actually works now exists
    req = next(iter(dv.requests.values()))
    assert req["new_name"] == "Add his son to the Carta portal"
    assert req["_new_sourcesignal_value"] == sig["new_engagementsignalid"]


def test_low_confidence_non_request_still_excluded(cfg, tmp_path, not_a_request):
    """No regression: ordinary weak-match chatter stays out of the queue."""
    dv = FakeDataverse(apply=True)
    run = make_run(cfg, tmp_path, dv)
    run._handle(make_msg(), OPP_META, {}, {}, {})

    sig = next(iter(dv.signals.values()))
    assert sig["new_matchstatus"] == EXCLUDED
    assert sig["new_noisereason"] == "low_confidence"
    assert sig["new_ismeaningful"] is False
    assert not dv.requests
    assert "rfi_rescued" not in run.counts


def test_outbound_request_is_not_rescued(cfg, tmp_path, asks):
    """Requests come from replies/incoming mail only — our own outbound copy of
    a thread must never open a ticket or be rescued into the queue."""
    dv = FakeDataverse(apply=True)
    run = make_run(cfg, tmp_path, dv)
    run._handle(make_msg(direction_out=True), OPP_META, {}, {}, {})

    sig = next(iter(dv.signals.values()))
    assert sig["new_direction"] == BASE + 1         # Outbound
    assert sig["new_matchstatus"] == EXCLUDED
    assert sig["new_noisereason"] == "low_confidence"
    assert not dv.requests
    assert "rfi_rescued" not in run.counts


def test_no_contact_never_reaches_the_rescue(cfg, tmp_path, asks):
    """Only mail associated with a contact is eligible: an unmatched sender is
    dropped before any of this, and opens no request."""
    dv = FakeDataverse(apply=True)
    run = make_run(cfg, tmp_path, dv)
    run._handle(make_msg(contacts={}), OPP_META, {}, {}, {})

    assert run.counts["no_contact"] == 1
    assert not dv.signals and not dv.requests


def test_rescued_signal_rerun_is_idempotent(cfg, tmp_path, asks):
    dv = FakeDataverse(apply=True)
    run = make_run(cfg, tmp_path, dv)
    run._handle(make_msg(), OPP_META, {}, {}, {})
    before = (dv.created, dv.patched, len(dv.requests))

    run2 = make_run(cfg, tmp_path, dv)
    run2._handle(make_msg(), OPP_META, {}, {}, {})
    assert (dv.created, dv.patched, len(dv.requests)) == before
    assert run2.counts["unchanged"] == 1
