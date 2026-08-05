"""WS1: noise-gate heuristics, threshold boundaries, v2 matcher boosters."""
from ingestion import noise
from ingestion.matching import disposition_for, resolve_opportunity_v2

ORG = ["exigentcap.com"]

META = {
    "live-1": {"name": "A - Fund X", "live": True, "prospectcode": "EXG-1040",
               "aliases": ["Fund X"]},
    "live-2": {"name": "A - Fund Y", "live": True, "prospectcode": "EXG-2000",
               "aliases": ["Fund Y"]},
    "dead-1": {"name": "A - Old Fund", "live": False, "prospectcode": "",
               "aliases": ["Old Fund"]},
}


# ── heuristics ────────────────────────────────────────────────────────────────
def test_list_headers_are_noise():
    assert noise.header_noise([{"name": "List-Unsubscribe", "value": "<x>"}]) \
        == "list_headers"
    assert noise.header_noise([{"name": "List-Id", "value": "news.wsj.com"}]) \
        == "list_headers"
    assert noise.header_noise([{"name": "Precedence", "value": "bulk"}]) \
        == "bulk_precedence"
    assert noise.header_noise([{"name": "X-Foo", "value": "bar"}]) is None


def test_blocklist_domain_and_subdomain(cfg):
    v, r = noise.gate("news@wsj.com", "Markets today", "", cfg.rules,
                      sender_is_matched_contact=False, sender_is_internal=False)
    assert (v, r) == ("noise", "blocklist:wsj.com")
    v, r = noise.gate("digest@mail.substack.com", "Weekly", "", cfg.rules,
                      sender_is_matched_contact=False, sender_is_internal=False)
    assert v == "noise" and r.startswith("blocklist:substack")


def test_unknown_external_sender_is_llm_candidate_not_dropped(cfg):
    v, r = noise.gate("someone@randomcorp.com", "Intro", "", cfg.rules,
                      sender_is_matched_contact=False, sender_is_internal=False)
    assert v == "candidate"          # spec 1.1: LLM check, never dropped outright


def test_matched_contact_sender_passes(cfg):
    v, _ = noise.gate("anna.lp@lpfund.com", "Question", "", cfg.rules,
                      sender_is_matched_contact=True, sender_is_internal=False)
    assert v == "ok"


def test_llm_label_mapping():
    assert noise.llm_label_to_noise_reason("newsletter_or_marketing") \
        == "llm:newsletter_or_marketing"
    assert noise.llm_label_to_noise_reason("investor_correspondence") is None


# ── threshold boundaries (spec 1.3) ──────────────────────────────────────────
def test_threshold_boundaries(cfg):
    assert disposition_for(100, cfg) == "auto_confirmed"
    assert disposition_for(85, cfg) == "auto_confirmed"
    assert disposition_for(84, cfg) == "needs_review"
    assert disposition_for(50, cfg) == "needs_review"
    assert disposition_for(49, cfg) == "noise"
    assert disposition_for(0, cfg) == "noise"


# ── v2 matcher boosters (spec 1.2) ───────────────────────────────────────────
def test_regarding_ground_truth_wins_over_everything():
    oid, method, conf = resolve_opportunity_v2(
        "EXG-2000 in subject", "", {"live-1"}, META,
        conv_opp="live-1", regarding_opp="dead-1")
    assert (oid, method, conf) == ("dead-1", "Regarding", 100)


def test_prospectcode_of_live_opp_is_explicit_100():
    oid, method, conf = resolve_opportunity_v2(
        "Re: EXG-1040 subscription docs", "", set(), META, conv_opp=None)
    assert (oid, method, conf) == ("live-1", "Explicit", 100)


def test_thread_inheritance_90():
    oid, method, conf = resolve_opportunity_v2(
        "Re: hello", "", {"live-1", "live-2"}, META, conv_opp="live-2")
    assert (oid, method, conf) == ("live-2", "Thread", 90)


def test_single_live_opp_auto_confirms():
    oid, method, conf = resolve_opportunity_v2(
        "hello", "", {"live-1", "dead-1"}, META, conv_opp=None)
    assert (oid, method, conf) == ("live-1", "ContactMatch", 90)  # dead-1 ignored


def test_multi_live_alias_narrows_to_content_60():
    oid, method, conf = resolve_opportunity_v2(
        "Question about Fund Y reporting", "", {"live-1", "live-2"}, META,
        conv_opp=None)
    assert (oid, method, conf) == ("live-2", "Content", 60)


def test_only_dead_opps_is_low_confidence(cfg):
    oid, method, conf = resolve_opportunity_v2(
        "hello", "", {"dead-1"}, META, conv_opp=None)
    assert conf == 30 and disposition_for(conf, cfg) == "noise"


def test_no_candidates_zero(cfg):
    oid, method, conf = resolve_opportunity_v2("hi", "", set(), META, None)
    assert (oid, conf) == (None, 0) and disposition_for(conf, cfg) == "noise"


# ── Tier-0 admin blasts (ir@ taxonomy, 2026-08-04) ───────────────────────────
def test_admin_blast_sender_is_noise(cfg):
    v, r = noise.gate("exigentcap.ir@apexgroup.com",
                      "Exigent HP Fund I-A LP Capital Call 23", "", cfg.rules,
                      sender_is_matched_contact=True,   # even if a CRM contact
                      sender_is_internal=False)
    assert (v, r) == ("noise", "admin_blast:exigentcap.ir@apexgroup.com")


def test_admin_blast_domain_suffix_is_noise(cfg):
    v, r = noise.gate("dse_na3@docusign.net", "Completed: forms", "", cfg.rules,
                      sender_is_matched_contact=False, sender_is_internal=False)
    assert (v, r) == ("noise", "admin_blast:docusign.net")


def test_named_fund_admin_staff_not_blast(cfg):
    # a person at the fund administrator is working correspondence, not a blast
    v, r = noise.gate("esther.berman@apexgroup.com", "Investor Portal Credentials",
                      "", cfg.rules,
                      sender_is_matched_contact=False, sender_is_internal=False)
    assert v == "candidate"   # falls through to the normal LLM triage path
