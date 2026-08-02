"""Directive acceptance checks against the latest export workbook (SynthBee)."""
import sys
from pathlib import Path

import pandas as pd

FUND = "Exigent SynthBee Holdings LP"
wb = sorted((Path(__file__).resolve().parent.parent / "reports")
            .glob("engagement_export_*.xlsx"))[-1]
print(f"checking {wb.name}\n")

bic = pd.read_excel(wb, "Ball In Court")
sb = bic[bic["Fund"] == FUND]
print(f"§1 Ball-in-Court (SynthBee): {len(sb)} in our court")
print(sb.head(6)[["Contact", "Waiting (biz days)", "Their inbound at",
                  "Our last outbound at"]].to_string(index=False))

req = pd.read_excel(wb, "Open Requests")
print(f"\n§2 Open requests: {len(req)} across "
      f"{req['Contact'].nunique()} contacts")
print(req[["Contact", "Routing", "Suggested owner", "Age (biz days)",
           "Overdue", "Title"]].to_string(index=False, max_colwidth=48))

clock = pd.read_excel(wb, "Deal Clock")
risk = clock[clock["At risk"] == True]  # noqa: E712
print(f"\n§3 Deal clock: {len(clock)} open prospects, {len(risk)} at risk "
      f"(days to close: {clock['Days to close'].iloc[0] if len(clock) else '—'})")
print(risk.head(8)[["Code", "Prospect", "Status", "Days stale",
                    "Stale / runway"]].to_string(index=False))

fun = pd.read_excel(wb, "Funnel", header=None)
print("\n§4 Funnel:")
print(fun.head(9).to_string(index=False, header=False))

lat = pd.read_excel(wb, "Latency Tail", header=None)
print("\n§5 Latency tail (summary block):")
print(lat.head(6).to_string(index=False, header=False))

hyg = pd.read_excel(wb, "Hygiene")
sbh = hyg[hyg["Fund"].astype(str).str.contains("SynthBee", na=False)]
print(f"\n§6 Hygiene (SynthBee): "
      f"{sbh['Flag'].value_counts().to_dict()}")
for f in ("delivery_failure", "suggested_closed_lost"):
    for _, r in sbh[sbh["Flag"] == f].iterrows():
        print(f"  {f}: {r['Contact']} — {str(r['Evidence'])[:90]}")

sig = pd.read_excel(wb, "Signals", usecols=["Fund", "Is Primary"])
print(f"\n§0.1 dedup: {len(sig)} signal rows, "
      f"{int(sig['Is Primary'].sum())} primary "
      f"({len(sig) - int(sig['Is Primary'].sum())} duplicate attributions)")
sys.exit(0)
