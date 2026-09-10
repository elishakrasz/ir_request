"""Scope rule (2026-09-10): mail from ANY Dynamics contact is brought in,
linked to a deal or not — the Eric Wietschner case, a live investor with zero
connection rows whose mail only arrived because an in-scope contact was cc'd.

HighPost / HIPstr are kept out by SUBJECT, not by contact: the same investor may
hold a HIPstr deal and a SynthBee one, and only the HIPstr traffic is unwanted.
"""
from ingestion.config import Rules
from ingestion.matching import is_excluded
from ingestion.sync import build_scope

FMT = "@OData.Community.Display.V1.FormattedValue"
RULES = Rules(excluded_subject_keywords=["hipstr", "highpost", "hp fund"])


def opp(oid, fund):
    return {"opportunityid": oid, "name": f"deal {oid}", "new_live": True,
            "_mint_fundorspv_value": f"f-{fund}", f"_mint_fundorspv_value{FMT}": fund,
            "_parentcontactid_value": None}


OPPS = [opp("o-synth", "Exigent SynthBee Holdings LP"),
        opp("o-hip2", "Exigent HIPstr Fund II LP")]


def test_unlinked_contact_is_in_scope():
    _, em = build_scope(OPPS, [], [{"contactid": "c1",
                                    "emailaddress1": "ejw722@gmail.com"}])
    assert em["ejw722@gmail.com"] == ("c1", set())


def test_hipstr_contact_stays_in_scope_now():
    """Contact-based exclusion is gone: only the subject decides."""
    conn = {"record1objecttypecode": 2, "_record1id_value": "c2",
            "_record2id_value": "o-hip2"}
    _, em = build_scope(OPPS, [conn], [{"contactid": "c2",
                                        "emailaddress1": "hip@investor.com"}])
    assert "hip@investor.com" in em


def test_subject_mentioning_the_funds_is_dropped():
    for subj in ("HP & HIPstr Reports & Statements", "RE: HighPost allocation",
                 "Exigent HP Fund I-A LP capital call", "hipstr statement"):
        assert is_excluded("investor@x.com", subj, "body", RULES), subj


def test_body_only_mention_is_NOT_dropped():
    """Subject line only — a signature or quoted thread must not kill the mail."""
    assert not is_excluded(
        "investor@x.com", "Re: SynthBee capital call",
        "Thanks. Separately, my HIPstr statement arrived. -- HighPost LLC", RULES)


def test_unrelated_subject_passes():
    assert not is_excluded("investor@x.com", "Re: Add Wire Instructions",
                           "please update", RULES)
