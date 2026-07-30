"""Response pairing (v2 WS3).

A response pair = a meaningful INBOUND message → the EARLIEST subsequent
meaningful OUTBOUND in the same conversation involving the same contact.
Latency is stored on the INBOUND row (business minutes, Sun–Thu Asia/Jerusalem)
— "how long until we replied to this". Unpaired inbounds keep latency null,
which is exactly what the awaiting-reply surface reads.

Noise (Excluded) and non-meaningful rows never participate — callers filter
before handing rows in; this module also guards.
"""
from .bizhours import business_minutes


def compute_response_pairs(signals: list[dict]) -> dict:
    """signals: rows of ONE conversation:
    [{id, contact, direction, ts (aware dt), latency, meaningful}]
    Returns {inbound_id: business_minutes} for currently-null inbounds that
    have a reply. Outbound-first threads produce no pair for the outbound."""
    out = {}
    rows = sorted((s for s in signals if s.get("meaningful", True)
                   and s["direction"] in ("Inbound", "Outbound")),
                  key=lambda s: s["ts"])
    by_contact: dict = {}
    for s in rows:
        by_contact.setdefault(s.get("contact"), []).append(s)
    for series in by_contact.values():
        outbounds = [s for s in series if s["direction"] == "Outbound"]
        for s in series:
            if s["direction"] != "Inbound" or s["latency"] is not None:
                continue
            reply = next((o for o in outbounds if o["ts"] > s["ts"]), None)
            if reply is not None:
                out[s["id"]] = business_minutes(s["ts"], reply["ts"])
    return out
