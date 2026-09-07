"""§4.2 recommended assignment (claude_ir.md): routing rule → last Exigent
responder → relationship owner; highest-weight non-excluded ACTIVE user wins.
Recommendation-only: the fields written are assigneerecommended/reason/
provenance — never ownerid. Fixture-driven: the fake answers the four
Dataverse reads the recommender makes; no live calls.
"""
import json
from datetime import datetime, timezone

from ingestion.classify import RfiResult
from ingestion.sync import SyncRun
from ingestion.tests.conftest import FakeDataverse, FakeGraph, ANNA, OPP1

FMT = "@OData.Community.Display.V1.FormattedValue"
YAEL = ("u-yael", "yforman@exigentcap.com", "Yael Forman")
DANA = ("u-dana", "dmizrahi@exigentcap.com", "Dana Mizrahi")
ERIC = ("u-eric", "edavis@exigentcap.com", "Eric Davis")
MICHAL = ("u-michal", "mravid@exigentcap.com", "Michal Ravid")
USERS = {u[0]: u for u in (YAEL, DANA, ERIC, MICHAL)}
TS = datetime(2026, 8, 5, 9, tzinfo=timezone.utc)


def route(name, category, user, tier="T3"):
    return {"new_name": name, f"new_matchtype{FMT}": "Category",
            f"new_action{FMT}": "Route", "new_matchvalue": category,
            f"new_tier{FMT}": tier,
            "new_handler": {"systemuserid": user[0], "fullname": user[2],
                            "internalemailaddress": user[1]}}


def exclude(user):
    return {"new_name": f"Exclude {user[2]}", f"new_matchtype{FMT}": "Handler",
            f"new_action{FMT}": "ExcludeHandler", "new_matchvalue": user[1]}


class RecoFake(FakeDataverse):
    """Answers the recommender's reads: active irrules (in priority order),
    systemusers, a contact's outbound signals (newest first), the contact's owner."""

    def __init__(self, rules=(), outbound_senders=(), owner=None,
                 owner_type="systemuser", disabled=(), rules_error=False):
        super().__init__(apply=True)
        self.rules = list(rules)
        self.outbound_senders = list(outbound_senders)
        self.owner, self.owner_type = owner, owner_type
        self.disabled = set(disabled)
        self.rules_error = rules_error
        self.queries = []

    def query(self, path):
        self.queries.append(path)
        if path.startswith("new_irrules?"):
            if self.rules_error:
                raise RuntimeError("403 principal lacks prvReadnew_irrule")
            out = []
            for r in self.rules:
                r = dict(r)
                h = r.get("new_handler")
                if h:
                    r["new_handler"] = dict(h, isdisabled=h["systemuserid"] in self.disabled)
                out.append(r)
            return out
        if path.startswith("systemusers?"):
            for uid, email, name in USERS.values():
                if f"systemuserid eq {uid}" in path:
                    return [{"systemuserid": uid, "internalemailaddress": email,
                             "fullname": name, "isdisabled": uid in self.disabled}]
                if f"internalemailaddress eq '{email}'" in path:
                    return [] if uid in self.disabled else [
                        {"systemuserid": uid, "internalemailaddress": email,
                         "fullname": name}]
            return []
        if path.startswith("new_engagementsignals?"):
            return [{"new_sender": s, "new_timestamputc": "2026-08-01T00:00:00Z"}
                    for s in self.outbound_senders]
        if path.startswith("contacts?"):
            if not self.owner:
                return [{"_ownerid_value": None}]
            return [{"_ownerid_value": self.owner,
                     "_ownerid_value@Microsoft.Dynamics.CRM.lookuplogicalname":
                         self.owner_type}]
        if path.startswith("new_ircategories?"):
            return []
        raise AssertionError(f"unexpected query {path}")


def make_run(cfg, tmp_path, dv):
    return SyncRun(cfg, FakeGraph([]), dv, apply=True, state_dir=tmp_path,
                   codeversion="test")


def recommend(cfg, tmp_path, dv, category="TaxDocs"):
    run = make_run(cfg, tmp_path, dv)
    rfi = RfiResult(is_info_request=True, category=category, description="K-1 please")
    return run._assignee_fields(rfi, {"subject": "K-1", "_ts": TS}, ANNA)


def picked(fields):
    b = fields.get("new_assigneerecommended@odata.bind", "")
    return b[b.index("(") + 1:-1] if b else None


def prov(fields):
    return json.loads(fields["new_assigneeprovenance"])


# ── precedence ───────────────────────────────────────────────────────────────
def test_routing_rule_outranks_responder_and_owner(cfg, tmp_path):
    dv = RecoFake(rules=[route("Tax to Yael", "TaxDocs", YAEL, "T4")],
                  outbound_senders=[DANA[1]], owner=ERIC[0])
    f = recommend(cfg, tmp_path, dv)
    assert picked(f) == YAEL[0]
    assert f["new_assigneereason"] == \
        "Recommended Yael Forman — routing rule 'Tax to Yael' for category TaxDocs."
    p = prov(f)
    assert p["source"] == "routing-rule" and p["category"] == "TaxDocs"
    # every input was evaluated and recorded, even though the rule won
    assert p["considered"] == ["routing-rule", "last-responder", "relationship-owner"]
    assert p["ts"] == "2026-08-05T09:00:00Z"


def test_last_responder_outranks_owner(cfg, tmp_path):
    dv = RecoFake(outbound_senders=[DANA[1], YAEL[1]], owner=ERIC[0])
    f = recommend(cfg, tmp_path, dv)
    assert picked(f) == DANA[0]                      # newest outbound wins
    assert prov(f)["source"] == "last-responder"
    assert "most recent Exigent responder" in f["new_assigneereason"]


def test_relationship_owner_is_last_resort(cfg, tmp_path):
    dv = RecoFake(owner=ERIC[0])
    f = recommend(cfg, tmp_path, dv)
    assert picked(f) == ERIC[0]
    assert prov(f)["source"] == "relationship-owner"


def test_earlier_priority_rule_wins_for_same_category(cfg, tmp_path):
    dv = RecoFake(rules=[route("Tax to Yael", "TaxDocs", YAEL),
                         route("Tax to Dana (old)", "TaxDocs", DANA)])
    assert picked(recommend(cfg, tmp_path, dv)) == YAEL[0]


def test_rule_for_other_category_does_not_apply(cfg, tmp_path):
    dv = RecoFake(rules=[route("Subs to Dana", "SubscriptionDocs", DANA)],
                  owner=ERIC[0])
    assert picked(recommend(cfg, tmp_path, dv, category="TaxDocs")) == ERIC[0]


def test_no_candidates_gives_no_recommendation(cfg, tmp_path):
    assert recommend(cfg, tmp_path, RecoFake()) == {}


# ── exclusions / inactive users ──────────────────────────────────────────────
def test_excluded_handler_is_skipped_at_every_level(cfg, tmp_path):
    """Michal is leaving: an ExcludeHandler rule drops her whether she came from a
    routing rule or from being the last responder — the next input wins."""
    dv = RecoFake(rules=[exclude(MICHAL), route("Tax to Michal", "TaxDocs", MICHAL)],
                  outbound_senders=[MICHAL[1]], owner=ERIC[0])
    f = recommend(cfg, tmp_path, dv)
    assert picked(f) == ERIC[0]
    assert prov(f)["considered"] == ["routing-rule", "last-responder",
                                     "relationship-owner"]


def test_excluded_only_candidate_gives_nothing(cfg, tmp_path):
    dv = RecoFake(rules=[exclude(MICHAL)], outbound_senders=[MICHAL[1]])
    assert recommend(cfg, tmp_path, dv) == {}


def test_disabled_routing_handler_is_skipped(cfg, tmp_path):
    dv = RecoFake(rules=[route("Tax to Yael", "TaxDocs", YAEL)],
                  outbound_senders=[DANA[1]], disabled={YAEL[0]})
    assert picked(recommend(cfg, tmp_path, dv)) == DANA[0]


def test_disabled_responder_and_owner_are_skipped(cfg, tmp_path):
    dv = RecoFake(outbound_senders=[DANA[1]], owner=ERIC[0],
                  disabled={DANA[0], ERIC[0]})
    assert recommend(cfg, tmp_path, dv) == {}


def test_shared_mailbox_sender_is_not_a_responder(cfg, tmp_path):
    """Outbound from ir@ resolves to no systemuser — look past it to the person."""
    dv = RecoFake(outbound_senders=["ir@exigentcap.com", DANA[1]])
    assert picked(recommend(cfg, tmp_path, dv)) == DANA[0]


def test_team_owned_contact_has_no_owner_candidate(cfg, tmp_path):
    dv = RecoFake(owner="team-0001", owner_type="team")
    assert recommend(cfg, tmp_path, dv) == {}


# ── inert / degraded modes ───────────────────────────────────────────────────
def test_unreadable_rules_disable_only_routing(cfg, tmp_path):
    dv = RecoFake(rules_error=True, outbound_senders=[DANA[1]])
    f = recommend(cfg, tmp_path, dv)
    assert picked(f) == DANA[0]                      # no exception, lower inputs work


def test_column_absent_is_inert(cfg, tmp_path):
    dv = RecoFake(rules=[route("Tax to Yael", "TaxDocs", YAEL)], owner=ERIC[0])
    dv.has_attribute = lambda entity, attr: False
    assert recommend(cfg, tmp_path, dv) == {}
    assert dv.queries == []                          # no reads either


def test_create_rfi_stores_recommendation_never_owner(cfg, tmp_path):
    dv = RecoFake(rules=[route("Tax to Yael", "TaxDocs", YAEL, "T2")])
    run = make_run(cfg, tmp_path, dv)
    rfi = RfiResult(is_info_request=True, category="TaxDocs", description="K-1 please")
    msg = {"subject": "K-1", "_ts": TS, "_hash": "a" * 64}
    run._create_rfi({"new_engagementsignalid": "sig-0001"}, msg, rfi, ANNA, OPP1)
    req = next(iter(dv.requests.values()))
    assert req["_new_assigneerecommended_value"] == YAEL[0]
    assert req["new_assigneereason"].startswith("Recommended Yael Forman")
    assert not any(k.startswith("ownerid") or k == "_ownerid_value" for k in req)
