"""Response-latency computation (spec Phase 2 step 8).

Pairs Inbound ↔ Outbound only — Internal signals are skipped entirely.
For an Outbound message: latency = minutes since the latest earlier Inbound in
the same conversation (and vice versa). Only signals whose stored latency is
null get a value (patch-only-where-null).
"""


def compute_latencies(signals: list[dict]) -> dict:
    """signals: [{id, direction: 'Inbound'|'Outbound'|'Internal', ts: datetime,
    latency: int|None}] for ONE conversation. Returns {id: minutes} for rows
    currently null that have a counterpart."""
    out = {}
    last_inbound = None
    last_outbound = None
    for s in sorted(signals, key=lambda s: s["ts"]):
        d = s["direction"]
        if d == "Internal":
            continue
        if d == "Outbound":
            if last_inbound is not None and s["latency"] is None:
                out[s["id"]] = int((s["ts"] - last_inbound).total_seconds() // 60)
            last_outbound = s["ts"]
        elif d == "Inbound":
            if last_outbound is not None and s["latency"] is None:
                out[s["id"]] = int((s["ts"] - last_outbound).total_seconds() // 60)
            last_inbound = s["ts"]
    return out
