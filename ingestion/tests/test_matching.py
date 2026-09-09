from datetime import datetime, timezone

from ingestion.matching import (looks_autoreply, match_contacts, resolve_opportunity)

T = datetime(2026, 7, 20, tzinfo=timezone.utc)
ORG = ["exigentcap.com"]
OPP_META = {
    "opp-1": {"name": "Fund II", "oppcode": "OPP-0042", "aliases": ["Fund II"],
              "active": True, "startdate": datetime(2026, 6, 1, tzinfo=timezone.utc)},
    "opp-2": {"name": "Fund III", "oppcode": "OPP-0043", "aliases": ["Fund III"],
              "active": True, "startdate": None},
}


def test_explicit_oppcode_wins():
    oid, method, conf, status = resolve_opportunity(
        T, "RE: OPP-0042 docs", "", {"opp-1", "opp-2"}, OPP_META, conv_opp="opp-2")
    assert (oid, method, status) == ("opp-1", "Explicit", "Confirmed") and conf >= 95


def test_thread_inheritance():
    oid, method, conf, status = resolve_opportunity(
        T, "RE: hello", "", {"opp-1", "opp-2"}, OPP_META, conv_opp="opp-2")
    assert (oid, method, conf, status) == ("opp-2", "Thread", 90, "Confirmed")


def test_single_active_contact_match():
    oid, method, conf, status = resolve_opportunity(
        T, "hello", "", {"opp-1"}, OPP_META, conv_opp=None)
    assert (oid, method, conf, status) == ("opp-1", "ContactMatch", 75, "Confirmed")


def test_contact_match_respects_monitoring_start():
    before = datetime(2026, 5, 1, tzinfo=timezone.utc)  # predates opp-1 start
    oid, method, conf, status = resolve_opportunity(
        before, "hello", "", {"opp-1"}, OPP_META, conv_opp=None)
    assert status in ("Suggested", "Unmatched") and method is None


def test_content_alias_narrows_to_suggested():
    oid, method, conf, status = resolve_opportunity(
        T, "About Fund III reporting", "", {"opp-1", "opp-2"}, OPP_META, conv_opp=None)
    assert (oid, method, conf, status) == ("opp-2", "Content", 60, "Suggested")


def test_ambiguous_never_silently_assigned():
    oid, method, conf, status = resolve_opportunity(
        T, "hello", "", {"opp-1", "opp-2"}, OPP_META, conv_opp=None)
    assert status == "Suggested" and method is None and conf < 60


def test_no_candidates_unmatched():
    oid, method, conf, status = resolve_opportunity(
        T, "hello", "", set(), {}, conv_opp=None)
    assert (oid, status) == (None, "Unmatched")


def test_contact_email_match_case_insensitive_and_plus_unmatched():
    email_map = {"anna.lp@lpfund.com": ("c1", {"opp-1"})}
    hit = match_contacts(["Anna.LP@LPFund.com".lower()], email_map, ORG)
    assert "c1" in hit
    miss = match_contacts(["anna.lp+tag@lpfund.com"], email_map, ORG)
    assert miss == {}   # plus-addressing deliberately unmatched


def test_internal_participants_never_match():
    email_map = {"ir@exigentcap.com": ("cX", {"opp-1"})}   # misconfigured contact
    assert match_contacts(["ir@exigentcap.com"], email_map, ORG) == {}
def test_all_three_email_addresses_are_valid():
    """Contacts reached on their 2nd/3rd address must match — 127 of the 719
    addresses in the live scope map come from emailaddress2/3 (e.g. Phil Rosen
    writing from jprosen@aol.com while philip.rosen@weil.com is on file)."""
    from ingestion.sync import build_scope
    _, email_map = build_scope([], [], [{
        "contactid": "c1", "fullname": "Phil Rosen",
        "emailaddress1": "jprosen@aol.com",
        "emailaddress2": "Philip.Rosen@Weil.com",     # mixed case on purpose
        "emailaddress3": "  prosen@family.example  ",  # padded on purpose
    }])
    assert set(email_map) == {"jprosen@aol.com", "philip.rosen@weil.com",
                              "prosen@family.example"}
    for addr in email_map:
        assert match_contacts([addr], email_map, ORG) == {"c1": set()}


def test_blank_secondary_addresses_are_not_indexed():
    from ingestion.sync import build_scope
    _, email_map = build_scope([], [], [{
        "contactid": "c2", "fullname": "One Address",
        "emailaddress1": "solo@lpfund.com",
        "emailaddress2": "", "emailaddress3": None,
    }])
    assert set(email_map) == {"solo@lpfund.com"}


def test_dateonly_monitoringstartdate_regression():
    """Dataverse DateOnly columns return bare dates ('2026-01-01') — build_scope
    must yield tz-aware datetimes or resolve_opportunity crashes (found live)."""
    from ingestion.sync import build_scope
    opp_meta, _ = build_scope(
        [{"opportunityid": "o1", "name": "X", "new_activemonitoring": True,
          "new_monitoringstartdate": "2026-01-01"}], [], [])
    oid, method, conf, status = resolve_opportunity(
        T, "hello", "", {"o1"}, opp_meta, conv_opp=None)
    assert (oid, method, status) == ("o1", "ContactMatch", "Confirmed")


def test_clip_counts_utf16_units_like_dataverse():
    """Dataverse rejected a 2000-codepoint snippet containing emoji (astral
    chars = 2 UTF-16 units). clip() must bound UTF-16 length, not len()."""
    from ingestion.sync import clip
    s = "x" * 1999 + "😀"          # len()==2000 but 2001 UTF-16 units
    out = clip(s, 2000)
    assert len(out.encode("utf-16-le")) // 2 <= 2000
    assert clip("short", 2000) == "short"
    assert clip("", 10) == ""


def test_autoreply_subject_and_sender(cfg):
    assert looks_autoreply("Automatic reply: hi", "anna@lpfund.com", cfg.rules)
    assert looks_autoreply("hi", "no-reply@bank.com", cfg.rules)
    assert not looks_autoreply("Quarterly question", "anna@lpfund.com", cfg.rules)
