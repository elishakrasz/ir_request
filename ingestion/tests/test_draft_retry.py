"""§4.4 resilience: a draft that failed at create time (LLM overload, network
blip) is retried on later ticks — bounded per run, per request (attempts kept
in draftprovenance) and by age. Dry runs and the broad signals-only route never
draft. No live calls: llm.draft_reply / llm.available are stubbed.
"""
import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from ingestion import llm
from ingestion.classify import RfiResult
from ingestion.sync import DRAFT_MAX_ATTEMPTS, SyncRun, draft_attempts
from ingestion.tests.conftest import FakeGraph, ANNA, OPP1
from ingestion.tests.test_assignee import YAEL, route
from ingestion.tests.test_draft import DraftFake, T3, T4, drafter  # noqa: F401

BASE = 100000000


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def pending(attempts):
    return json.dumps({"draft": "pending", "attempts": attempts, "tier": "T3",
                       "ts": "2026-08-05T09:00:00Z"})


class RetryFake(DraftFake):
    """Adds the open-request read the retry pass makes; rows are also seeded
    into .requests so the patch lands somewhere."""

    def __init__(self, request_rows=(), **kw):
        super().__init__(**kw)
        self.request_rows = list(request_rows)
        for r in self.request_rows:
            self.requests[r["new_inforequestid"]] = dict(r)

    def query(self, path):
        if path.startswith("new_inforequests?"):
            self.queries.append(path)
            return [dict(r) for r in self.request_rows]
        return super().query(path)


def req_row(rid="req-0001", tier=T3, provenance=None, days_ago=1):
    return {"new_inforequestid": rid, "new_name": "CPA needs the 2025 K-1",
            "new_category": BASE + 10,                       # TaxDocs
            "new_receiveddate": iso(datetime.now(timezone.utc) - timedelta(days=days_ago)),
            "new_drafttier": tier, "new_draftprovenance": provenance,
            "new_draftreply": None, "_new_contact_value": ANNA}


def make_run(cfg, tmp_path, dv, apply=True):
    return SyncRun(cfg, FakeGraph([]), dv, apply=apply, state_dir=tmp_path,
                   codeversion="test")


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)


def test_draft_attempts_reader():
    assert draft_attempts(None) == 0
    assert draft_attempts("not json") == 0
    assert draft_attempts(json.dumps({"model": "x", "tier": "T3"})) == 0   # a real draft
    assert draft_attempts(pending(2)) == 2


def test_failed_draft_at_create_records_pending_attempt(cfg, tmp_path, drafter):
    drafter["text"] = None                          # LLM overloaded / declined
    dv = RetryFake(rules=[route("Tax to Yael", "TaxDocs", YAEL, "T4")])
    run = make_run(cfg, tmp_path, dv)
    rfi = RfiResult(is_info_request=True, category="TaxDocs", description="K-1")
    msg = {"subject": "K-1", "_ts": datetime(2026, 8, 5, 9, tzinfo=timezone.utc),
           "_hash": "r" * 64}
    run._create_rfi({"new_engagementsignalid": "sig-0001"}, msg, rfi, ANNA, OPP1)
    req = next(iter(dv.requests.values()))
    assert req["new_drafttier"] == T4 and "new_draftreply" not in req
    p = json.loads(req["new_draftprovenance"])
    assert p["draft"] == "pending" and p["attempts"] == 1 and p["tier"] == "T4"


def test_retry_drafts_open_t3_t4_without_draft(cfg, tmp_path, drafter, api_key):
    dv = RetryFake(request_rows=[req_row(provenance=pending(1))])
    run = make_run(cfg, tmp_path, dv)
    run._draft_retry_pass()
    assert dv.requests["req-0001"]["new_draftreply"] == "Draft body."
    assert json.loads(dv.requests["req-0001"]["new_draftprovenance"])["tier"] == "T3"
    assert run.counts["draft_retried"] == 1 and dv.patched == 1
    q = [q for q in dv.queries if q.startswith("new_inforequests?")][0]
    assert "new_draftreply eq null" in q
    assert f"new_drafttier eq {T3}" in q and f"new_drafttier eq {T4}" in q
    assert f"new_status ne {BASE + 4}" in q and f"new_status ne {BASE + 5}" in q
    assert "new_receiveddate ge " in q and "$top=10" in q


def test_retry_gives_up_after_max_attempts(cfg, tmp_path, drafter, api_key):
    dv = RetryFake(request_rows=[req_row(provenance=pending(DRAFT_MAX_ATTEMPTS))])
    make_run(cfg, tmp_path, dv)._draft_retry_pass()
    assert drafter["calls"] == [] and dv.patched == 0


def test_failed_retry_increments_attempts(cfg, tmp_path, drafter, api_key):
    drafter["text"] = None
    dv = RetryFake(request_rows=[req_row(provenance=pending(1))])
    run = make_run(cfg, tmp_path, dv)
    run._draft_retry_pass()
    req = dv.requests["req-0001"]
    assert req["new_draftreply"] is None
    assert json.loads(req["new_draftprovenance"])["attempts"] == 2
    assert run.counts["draft_retry_failed"] == 1 and "draft_retried" not in run.counts


def test_retry_skips_without_api_key_or_column(cfg, tmp_path, drafter):
    dv = RetryFake(request_rows=[req_row()])           # llm.available → False (autouse)
    make_run(cfg, tmp_path, dv)._draft_retry_pass()
    assert dv.queries == [] and dv.patched == 0


def test_retry_skips_when_column_absent(cfg, tmp_path, drafter, api_key):
    dv = RetryFake(request_rows=[req_row()])
    dv.has_attribute = lambda entity, attr: False
    make_run(cfg, tmp_path, dv)._draft_retry_pass()
    assert dv.queries == [] and dv.patched == 0


def test_retry_only_on_request_route_and_never_in_dry_run(cfg, tmp_path, drafter,
                                                          api_key):
    dv = RetryFake(request_rows=[req_row()])
    make_run(dataclasses.replace(cfg, create_requests=False), tmp_path, dv)._draft_retry_pass()
    assert dv.queries == []
    make_run(cfg, tmp_path, dv, apply=False)._draft_retry_pass()
    assert dv.queries == [] and drafter["calls"] == []


def test_query_failure_is_logged_not_raised(cfg, tmp_path, drafter, api_key):
    dv = RetryFake()

    def boom(path):
        raise RuntimeError("503")
    dv.query = boom
    make_run(cfg, tmp_path, dv)._draft_retry_pass()   # no exception
    assert dv.patched == 0
