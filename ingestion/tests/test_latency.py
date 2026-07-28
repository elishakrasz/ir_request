from datetime import datetime, timezone

from ingestion.latency import compute_latencies


def ts(h, m=0):
    return datetime(2026, 7, 20, h, m, tzinfo=timezone.utc)


def test_outbound_latency_from_latest_earlier_inbound():
    sigs = [
        {"id": "a", "direction": "Inbound", "ts": ts(9), "latency": None},
        {"id": "b", "direction": "Outbound", "ts": ts(10, 30), "latency": None},
    ]
    assert compute_latencies(sigs) == {"b": 90}


def test_inbound_reply_latency_vice_versa():
    sigs = [
        {"id": "a", "direction": "Outbound", "ts": ts(9), "latency": None},
        {"id": "b", "direction": "Inbound", "ts": ts(9, 45), "latency": None},
    ]
    assert compute_latencies(sigs) == {"b": 45}


def test_internal_skipped_entirely():
    sigs = [
        {"id": "a", "direction": "Inbound", "ts": ts(9), "latency": None},
        {"id": "i", "direction": "Internal", "ts": ts(9, 30), "latency": None},
        {"id": "b", "direction": "Outbound", "ts": ts(10), "latency": None},
    ]
    out = compute_latencies(sigs)
    assert out == {"b": 60} and "i" not in out


def test_patch_only_where_null():
    sigs = [
        {"id": "a", "direction": "Inbound", "ts": ts(9), "latency": None},
        {"id": "b", "direction": "Outbound", "ts": ts(10), "latency": 60},
    ]
    assert compute_latencies(sigs) == {}


def test_uses_latest_earlier_counterpart():
    sigs = [
        {"id": "a", "direction": "Inbound", "ts": ts(8), "latency": None},
        {"id": "b", "direction": "Inbound", "ts": ts(9, 50), "latency": None},
        {"id": "c", "direction": "Outbound", "ts": ts(10), "latency": None},
    ]
    assert compute_latencies(sigs)["c"] == 10   # from 09:50, not 08:00
