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


def test_sane_deadline_bumps_wrong_year():
    from ingestion.sync import sane_deadline
    received = ts(31, 9)                       # 2026-07-31
    # classifier emitted last year for "by 8/15" → bumped to 2026
    assert sane_deadline("2025-08-15", received) == "2026-08-15"
    # plausible deadlines pass through
    assert sane_deadline("2026-08-15", received) == "2026-08-15"
    # implausible even after bump → dropped
    assert sane_deadline("2024-01-01", received) is None
    assert sane_deadline(None, received) is None
    assert sane_deadline("not-a-date", received) is None


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


def test_create_rfi_writes_v2_taxonomy_fields(cfg, tmp_path):
    """Classifier v2: secondary/third_party/confidence land on the request row
    (FakeDataverse reports the columns as existing)."""
    dv = FakeDataverse(apply=True)
    run = make_run(cfg, tmp_path, dv)
    rfi = RfiResult(is_info_request=True, category="TaxDocs",
                    secondary="Valuation", third_party=True, confidence=88,
                    description="CPA needs the 2024 K-1 for River Oaks")
    msg = {"subject": "K-1s", "_ts": ts(30, 9), "_hash": "k" * 64}
    run._create_rfi({"new_engagementsignalid": "sig-0002"}, msg, rfi, ANNA, OPP1)
    req = next(iter(dv.requests.values()))
    assert req["new_category"] == BASE + 10            # TaxDocs (append-only)
    assert req["new_secondarycategory"] == BASE + 2    # Valuation
    assert req["new_thirdparty"] is True
    assert req["new_classifierconfidence"] == 88


def test_nda_category_writes_when_column_present(cfg, tmp_path):
    """v3: NDA is a real option value (base+13) once the import has landed."""
    dv = FakeDataverse(apply=True)                     # has_attribute → True
    run = make_run(cfg, tmp_path, dv)
    rfi = RfiResult(is_info_request=True, category="NDA",
                    description="Please countersign the mutual NDA")
    msg = {"subject": "NDA", "_ts": ts(30, 9), "_hash": "n" * 64}
    run._create_rfi({"new_engagementsignalid": "sig-nda"}, msg, rfi, ANNA, OPP1)
    req = next(iter(dv.requests.values()))
    assert req["new_category"] == BASE + 13            # NDA (append-only)


def test_nda_folds_to_sideletter_before_v3_import(cfg, tmp_path):
    """PROD-before-import: NDA has no option value yet → folds to Legal-SideLetter
    (index 5) rather than writing an unknown option (which would 400)."""
    dv = FakeDataverse(apply=True)
    dv.has_attribute = lambda entity, attr: False
    run = make_run(cfg, tmp_path, dv)
    rfi = RfiResult(is_info_request=True, category="NDA", description="NDA please")
    msg = {"subject": "NDA", "_ts": ts(30, 9), "_hash": "m" * 64}
    run._create_rfi({"new_engagementsignalid": "sig-nda2"}, msg, rfi, ANNA, OPP1)
    req = next(iter(dv.requests.values()))
    assert req["new_category"] == BASE + 5             # Legal-SideLetter


def _matched_inbound_msg():
    """External inbound message that resolves Confirmed to OPP1 via regarding_opp,
    so a request would be created absent any suppression."""
    return {
        "id": "graph-tp-1", "subject": "Transfer instructions",
        "bodyPreview": "We need IBAN and ISIN before delivery.",
        "conversationId": "CONV-TP-1", "internetMessageId": "<tp1@x>",
        "from": {"emailAddress": {"address": "team@bankleumi.com", "name": "P&D"}},
        "webLink": "https://outlook/tp1",
        "_ts": datetime(2026, 8, 4, 9, tzinfo=timezone.utc),
        "_sender": "team@bankleumi.com", "_recipients": ["ir@exigentcap.com"],
        "_participants": ["team@bankleumi.com", "ir@exigentcap.com"],
        "_hash": "t" * 64, "_mailbox": "ir@exigentcap.com", "_folder": "inbox",
        "_contacts": {ANNA: {OPP1}}, "_intake": False, "_noise_reason": None,
    }


def test_third_party_request_suppressed_signal_kept(cfg, tmp_path, monkeypatch):
    """suppress_third_party_requests: a third-party sender logs a signal but
    opens no ticket; the skip is counted."""
    from ingestion import sync as sync_mod
    dv = FakeDataverse(apply=True)
    cfg.suppress_third_party_requests = True
    cfg.create_requests = True
    run = make_run(cfg, tmp_path, dv)
    monkeypatch.setattr(sync_mod, "classify", lambda subj, body: RfiResult(
        is_info_request=True, third_party=True, category="LiquidityTransfer",
        description="Bank asks for IBAN/ISIN"))
    msg = _matched_inbound_msg()
    run._handle(msg, {}, {}, {}, {}, regarding_map={msg["internetMessageId"]: OPP1})
    assert not dv.requests                              # no ticket opened
    assert dv.signals                                   # signal still logged
    assert run.counts.get("rfi_skipped_thirdparty") == 1


def test_non_third_party_request_still_created(cfg, tmp_path, monkeypatch):
    """Control: with suppression on, a NON-third-party sender still opens a ticket."""
    from ingestion import sync as sync_mod
    dv = FakeDataverse(apply=True)
    cfg.suppress_third_party_requests = True
    cfg.create_requests = True
    run = make_run(cfg, tmp_path, dv)
    monkeypatch.setattr(sync_mod, "classify", lambda subj, body: RfiResult(
        is_info_request=True, third_party=False, category="SubscriptionDocs",
        description="Investor asks to re-send subscription docs"))
    msg = _matched_inbound_msg()
    run._handle(msg, {}, {}, {}, {}, regarding_map={msg["internetMessageId"]: OPP1})
    assert dv.requests                                  # ticket opened
    assert run.counts.get("rfi_skipped_thirdparty") is None


def test_v2_category_folds_to_other_before_solution_import(cfg, tmp_path):
    """PROD-before-import: v2-only categories must not write unknown option
    values — they fold to Other and the v2 columns are skipped."""
    dv = FakeDataverse(apply=True)
    dv.has_attribute = lambda entity, attr: False   # env predates the import
    run = make_run(cfg, tmp_path, dv)
    rfi = RfiResult(is_info_request=True, category="CapitalCall",
                    secondary="TaxDocs", third_party=True, confidence=90,
                    description="Wire sent, please confirm receipt")
    msg = {"subject": "Capital call", "_ts": ts(30, 9), "_hash": "c" * 64}
    run._create_rfi({"new_engagementsignalid": "sig-0003"}, msg, rfi, ANNA, OPP1)
    req = next(iter(dv.requests.values()))
    assert req["new_category"] == BASE + 8            # Other
    assert "new_thirdparty" not in req
    assert "new_secondarycategory" not in req
