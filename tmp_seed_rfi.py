"""WS6 acceptance: seeded emails → correct urgency tags (live LLM check)."""
import json
import sys
sys.path.insert(0, ".")
from ingestion.llm import classify_request

SEEDS = [
    ("Q2 capital account statement needed",
     "Hi team, could you please send over the Q2 capital account statement for "
     "our records? We need it by August 5th, 2026 for our auditors. Thanks, Anna"),
    ("URGENT: wire instructions",
     "We are trying to complete the subscription today — please send the wire "
     "instructions ASAP, this is urgent. Best, Michael"),
    ("Question about the fund's sector exposure",
     "Hi, quick question when you have a moment — roughly what share of the "
     "portfolio is exposed to the industrials sector? No rush. Regards, David"),
]
EXPECT = ["explicit_deadline", "urgent_language", "none"]

ok = True
for (subj, body), want in zip(SEEDS, EXPECT):
    r = classify_request(subj, body)
    got = (r or {}).get("urgency")
    is_req = (r or {}).get("is_request")
    mark = "✓" if got == want and is_req else "✗"
    ok &= got == want and bool(is_req)
    print(f"{mark} {subj[:44]:44} → request={is_req} urgency={got} "
          f"deadline={(r or {}).get('deadline')} cat={(r or {}).get('category')}")
    print(f"    desc: {(r or {}).get('description')}")
sys.exit(0 if ok else 1)
