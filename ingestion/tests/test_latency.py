"""WS3: inbound-anchored response pairing + Sun–Thu Asia/Jerusalem business time.

July 2026 anchors (IDT = UTC+3): Mon 20, Thu 23, Fri 24, Sat 25, Sun 26.
"""
from datetime import datetime, timezone

from ingestion.bizhours import business_minutes
from ingestion.latency import compute_response_pairs


def utc(d, h, m=0):
    return datetime(2026, 7, d, h, m, tzinfo=timezone.utc)


def sig(i, contact, direction, ts, latency=None, meaningful=True):
    return {"id": i, "contact": contact, "direction": direction, "ts": ts,
            "latency": latency, "meaningful": meaningful}


# ── business minutes ─────────────────────────────────────────────────────────
def test_same_workday():
    assert business_minutes(utc(20, 9), utc(20, 10, 30)) == 90   # Monday


def test_cross_midnight_workdays():
    # Mon 23:00 IDT (20:00Z) → Tue 01:00 IDT (22:00Z): both sides workdays
    assert business_minutes(utc(20, 20), utc(20, 22)) == 120


def test_cross_weekend_fri_sat_excluded():
    # Thu 23:00 IDT (Jul 23 20:00Z) → Sun 01:00 IDT (Jul 25 22:00Z)
    # = 60 min Thursday + 0 (Fri, Sat) + 60 min Sunday
    assert business_minutes(utc(23, 20), utc(25, 22)) == 120


def test_friday_is_weekend():
    # entirely within Friday Jerusalem → zero
    assert business_minutes(utc(24, 6), utc(24, 12)) == 0


def test_sunday_is_workday():
    assert business_minutes(utc(26, 6), utc(26, 8)) == 120


def test_end_before_start_zero():
    assert business_minutes(utc(20, 10), utc(20, 9)) == 0


# ── pairing ──────────────────────────────────────────────────────────────────
def test_inbound_pairs_with_earliest_subsequent_outbound():
    sigs = [sig("in1", "c1", "Inbound", utc(20, 9)),
            sig("out1", "c1", "Outbound", utc(20, 10, 30)),
            sig("out2", "c1", "Outbound", utc(20, 12))]
    assert compute_response_pairs(sigs) == {"in1": 90}   # earliest, not latest


def test_no_reply_no_pair():
    sigs = [sig("in1", "c1", "Inbound", utc(20, 9))]
    assert compute_response_pairs(sigs) == {}


def test_multiple_inbounds_before_one_outbound_each_pair():
    sigs = [sig("in1", "c1", "Inbound", utc(20, 8)),
            sig("in2", "c1", "Inbound", utc(20, 9)),
            sig("out", "c1", "Outbound", utc(20, 10))]
    assert compute_response_pairs(sigs) == {"in1": 120, "in2": 60}


def test_outbound_first_thread_no_pair():
    sigs = [sig("out", "c1", "Outbound", utc(20, 8)),
            sig("in1", "c1", "Inbound", utc(20, 9))]
    assert compute_response_pairs(sigs) == {}   # inbound awaits OUR reply


def test_patch_only_where_null():
    sigs = [sig("in1", "c1", "Inbound", utc(20, 9), latency=45),
            sig("out", "c1", "Outbound", utc(20, 10))]
    assert compute_response_pairs(sigs) == {}


def test_same_contact_only():
    sigs = [sig("in1", "c1", "Inbound", utc(20, 9)),
            sig("out", "c2", "Outbound", utc(20, 10))]   # different contact
    assert compute_response_pairs(sigs) == {}


def test_noise_and_internal_skipped():
    sigs = [sig("in1", "c1", "Inbound", utc(20, 9)),
            sig("bot", "c1", "Outbound", utc(20, 9, 30), meaningful=False),
            sig("int", "c1", "Internal", utc(20, 9, 45)),
            sig("out", "c1", "Outbound", utc(20, 11))]
    assert compute_response_pairs(sigs) == {"in1": 120}  # real reply, not the bot


def test_cross_weekend_pairing_uses_business_minutes():
    sigs = [sig("in1", "c1", "Inbound", utc(23, 20)),     # Thu 23:00 IDT
            sig("out", "c1", "Outbound", utc(25, 22))]    # Sun 01:00 IDT
    assert compute_response_pairs(sigs) == {"in1": 120}
