"""Rough bucket sizing over the deduped human threads (subject+preview keywords).
Buckets overlap; a thread counts in every bucket it matches. Approximate by design.
"""
import re
from pathlib import Path

HUMAN = Path(__file__).resolve().parents[2] / "reports" / "ir_human.tsv"

BUCKETS = {
    "tax-docs (K-1 etc.)": r"\bk-?1s?\b|schedule k|tax (return|information|doc)|w-?8|w-?9|1099",
    "capital call (payment/questions)": r"capital call",
    "statements & reporting": r"capital account|financial statements|quarterly report|statement",
    "valuation / shares / performance": r"valuation|share|nav\b|price|% holding|beneficial",
    "subscription & onboarding": r"subscription|carta|synthbee|account opening|onboard|sign(ature)? (the|and)|w8-?ben",
    "liquidity / sell / distribution": r"\bsell\b|liquidat|redemption|proceeds|distribution|buy(ing)? (an )?additional|secondary",
    "transfers (shares/DTC/brokerage)": r"dtc|stock transfer|securities transfer|brokerage|transfer (the )?shares|receiving firm",
    "portal & contact admin": r"portal|password|access|email address|distribution list|address change|add .*email|remove",
    "meetings / calls": r"zoom|meeting|schedule a call|call on|speak",
    "fund status / general updates": r"update on|any update|what is (this|the plan)|understand|explain",
}

rows = HUMAN.read_text(encoding="utf-8").splitlines()[1:]
total = len(rows)
print(f"total deduped human threads: {total}")
for name, pat in BUCKETS.items():
    rx = re.compile(pat, re.I)
    n = sum(1 for r in rows if rx.search(r))
    print(f"  {n:4d}  {name}")
