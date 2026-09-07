"""§4.4 draft-reply recommendation (claude_ir.md): T3/T4 only, built from the
contact's prior outbound + cross-investor exemplars (style reference only),
STORED on the request and never sent; tier comes from the routing rule (T1 for
unrouted). The LLM call is stubbed — no live calls.
"""
import json
from datetime import datetime, timezone

import pytest

from ingestion import llm
from ingestion.classify import RfiResult
from ingestion.sync import RECO_PROMPT_VERSION, SyncRun
from ingestion.tests.conftest import FakeDataverse, FakeGraph, ANNA, OPP1
from ingestion.tests.test_assignee import FMT, YAEL, DANA, route

BASE = 100000000
T1, T2, T3, T4 = BASE, BASE + 1, BASE + 2, BASE + 3
TS = datetime(2026, 8, 5, 9, tzinfo=timezone.utc)
PRIOR = "Hi Anna, attached is the Q2 capital account statement you asked for."
HOUSE = "Dear investor, thank you for reaching out — a member of the team will follow up."
SAME_CAT = "Dear investor, K-1s for 2025 are expected from the preparer in March."


class DraftFake(FakeDataverse):
    """Answers the drafter's reads: routing rules, the contact's own outbound
    (prior correspondence), other contacts' outbound (exemplars: same category
    first, then the house voice)."""

    def __init__(self, rules=(), prior=(PRIOR,), same_category=(), house=(HOUSE,)):
        super().__init__(apply=True)
        self.rules = list(rules)
        self.prior, self.same_category, self.house = list(prior), list(same_category), list(house)
        self.queries = []

    def query(self, path):
        self.queries.append(path)
        if path.startswith("new_irrules?"):
            return [dict(r) for r in self.rules]
        if path.startswith("new_engagementsignals?"):
            if f"_new_contact_value ne {ANNA}" in path:            # exemplars
                src = self.same_category if "new_category eq" in path else self.house
                return [{"new_snippet": s} for s in src]
            if f"_new_contact_value eq {ANNA}" in path:            # prior / responder
                if "new_snippet" in path:
                    return [{"new_snippet": s, "new_engagementsignalid": f"sig-p{i}"}
                            for i, s in enumerate(self.prior, 1)]
                return []
        if path.startswith(("systemusers?", "contacts?", "new_ircategories?")):
            return []
        raise AssertionError(f"unexpected query {path}")


@pytest.fixture
def drafter(monkeypatch):
    """Stub llm.draft_reply: records calls, returns `text` (settable)."""
    calls = []
    state = {"text": "Draft body."}

    def fake(subject, summary, prior, exemplars=None, log=print):
        calls.append({"subject": subject, "summary": summary, "prior": list(prior),
                      "exemplars": list(exemplars or [])})
        return state["text"]

    monkeypatch.setattr(llm, "draft_reply", fake)
    state["calls"] = calls
    return state


def make_run(cfg, tmp_path, dv):
    return SyncRun(cfg, FakeGraph([]), dv, apply=True, state_dir=tmp_path,
                   codeversion="test")


def draft(cfg, tmp_path, dv, tier, category="TaxDocs"):
    rfi = RfiResult(is_info_request=True, category=category,
                    description="CPA needs the 2025 K-1")
    return make_run(cfg, tmp_path, dv)._draft_fields(
        rfi, {"subject": "K-1 request", "_ts": TS}, ANNA, tier)


def test_t1_and_t2_get_no_draft(cfg, tmp_path, drafter):
    dv = DraftFake()
    assert draft(cfg, tmp_path, dv, "T1") == {}
    assert draft(cfg, tmp_path, dv, "T2") == {}
    assert drafter["calls"] == [] and dv.queries == []


def test_t3_draft_is_stored_with_provenance(cfg, tmp_path, drafter):
    f = draft(cfg, tmp_path, DraftFake(), "T3")
    assert f["new_draftreply"] == "Draft body."
    p = json.loads(f["new_draftprovenance"])
    assert p["model"] == llm.REQUEST_MODEL and p["prompt_ver"] == RECO_PROMPT_VERSION
    assert p["tier"] == "T3" and p["source_signals"] == ["sig-p1"]
    assert p["exemplars"] == 1 and p["ts"] == "2026-08-05T09:00:00Z"
    call = drafter["calls"][0]
    assert call["subject"] == "K-1 request" and call["summary"] == "CPA needs the 2025 K-1"
    assert call["prior"] == [PRIOR] and call["exemplars"] == [HOUSE]


def test_exemplars_prefer_same_category_then_house_voice(cfg, tmp_path, drafter):
    draft(cfg, tmp_path, DraftFake(same_category=[SAME_CAT]), "T4")
    assert drafter["calls"][-1]["exemplars"] == [SAME_CAT]
    draft(cfg, tmp_path, DraftFake(same_category=[]), "T4")
    assert drafter["calls"][-1]["exemplars"] == [HOUSE]


def test_exemplars_never_include_this_contact(cfg, tmp_path, drafter):
    dv = DraftFake()
    draft(cfg, tmp_path, dv, "T3")
    ex_queries = [q for q in dv.queries if f"_new_contact_value ne {ANNA}" in q]
    assert ex_queries and all("new_ismeaningful eq true" in q for q in ex_queries)


def test_no_prior_history_still_drafts(cfg, tmp_path, drafter):
    f = draft(cfg, tmp_path, DraftFake(prior=(), house=()), "T3")
    assert f["new_draftreply"] == "Draft body."
    assert json.loads(f["new_draftprovenance"])["source_signals"] == []
    assert drafter["calls"][0]["prior"] == [] and drafter["calls"][0]["exemplars"] == []


def test_column_absent_is_inert(cfg, tmp_path, drafter):
    dv = DraftFake()
    dv.has_attribute = lambda entity, attr: False
    assert draft(cfg, tmp_path, dv, "T4") == {}
    assert drafter["calls"] == []


def _create(cfg, tmp_path, dv, category):
    run = make_run(cfg, tmp_path, dv)
    rfi = RfiResult(is_info_request=True, category=category, description="Please help")
    msg = {"subject": "Help", "_ts": TS, "_hash": "d" * 64}
    run._create_rfi({"new_engagementsignalid": "sig-0001"}, msg, rfi, ANNA, OPP1)
    return next(iter(dv.requests.values()))


def test_create_rfi_tier_from_rule_drafts_only_t3_t4(cfg, tmp_path, drafter):
    rules = [route("Tax to Yael", "TaxDocs", YAEL, "T4"),
             route("Meeting to Dana", "Meeting", DANA, "T2")]
    req = _create(cfg, tmp_path, DraftFake(rules=rules), "TaxDocs")
    assert req["new_drafttier"] == T4 and req["new_draftreply"] == "Draft body."
    req = _create(cfg, tmp_path, DraftFake(rules=rules), "Meeting")
    assert req["new_drafttier"] == T2 and "new_draftreply" not in req
    req = _create(cfg, tmp_path, DraftFake(rules=rules), "Other")   # unrouted
    assert req["new_drafttier"] == T1 and "new_draftreply" not in req
    assert len(drafter["calls"]) == 1
