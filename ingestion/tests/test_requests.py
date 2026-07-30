"""WS6: deterministic escalation boundaries (Sun–Thu business days) and
urgency mapping from classifier output. LLM behavior itself is verified by the
live seeded-email check (see REVISION_NOTES), not unit tests."""
from datetime import datetime, timezone

from ingestion.classify import RfiResult
from ingestion.requests_logic import escalation

# Monday 2026-07-20 06:00 UTC (09:00 IDT) as the creation anchor
CREATED = datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc)


def at(d, h):
    return datetime(2026, 7, d, h, tzinfo=timezone.utc)


def test_fresh_within_two_business_days():
    # Wed 06:00 UTC = exactly 2 business days (2880 biz min) → still fresh (>)
    assert escalation(CREATED, None, at(22, 6)) == "fresh"


def test_amber_after_two_business_days():
    assert escalation(CREATED, None, at(22, 7)) == "amber"     # 2bd + 1h


def test_red_after_five_business_days():
    # From Mon 20 09:00 IDT, 5 business days (7200 biz min) elapse at
    # Mon 27 09:00 IDT (Fri+Sat contribute zero) — one hour later → red
    assert escalation(CREATED, None, at(26, 7)) == "amber"   # ~4bd → still amber
    assert escalation(CREATED, None, at(27, 7)) == "red"


def test_weekend_does_not_escalate():
    # created Thu 23 06:00; checked Sun 26 05:00 → Thu counts (~18h)+0+0+Sun(8h)
    # ≈ 1.1 business days → fresh despite 3 calendar days
    assert escalation(at(23, 6), None, at(26, 5)) == "fresh"


def test_past_explicit_deadline_is_red_regardless_of_age():
    deadline = at(21, 12)
    assert escalation(CREATED, deadline, at(21, 13)) == "red"  # 1 day old


def test_future_deadline_does_not_force_red():
    deadline = at(30, 12)
    assert escalation(CREATED, deadline, at(21, 6)) == "fresh"


def test_urgency_default_mapping():
    r = RfiResult()
    assert (r.urgency, r.deadline, r.is_info_request) == ("none", None, False)
