"""One-off READ-ONLY check: did the EngagementDashboard solution land in PROD?"""
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solution"))
from provision import load_env, get_token  # noqa: E402

env = load_env(Path(__file__).resolve().parents[1] / ".env")
PROD = "https://exigentcrmprod.crm4.dynamics.com"
t = {"Authorization": "Bearer " + get_token(env, PROD + "/")}
api = PROD + "/api/data/v9.2"

r = requests.get(api + "/solutions?$select=uniquename,version,ismanaged,installedon"
                 "&$filter=uniquename eq 'EngagementDashboard'", headers=t, timeout=60)
print("solution:", r.json().get("value", r.text[:200]))

for lname in ("new_engagementsignal", "new_inforequest"):
    r = requests.get(f"{api}/EntityDefinitions(LogicalName='{lname}')"
                     "?$select=LogicalName,SchemaName", headers=t, timeout=60)
    print(lname, "->", r.status_code if r.status_code != 200 else "EXISTS")

r = requests.get(api + "/EntityDefinitions(LogicalName='opportunity')/Attributes"
                 "?$select=LogicalName&$filter=LogicalName eq 'new_activemonitoring'",
                 headers=t, timeout=60)
print("opportunity.new_activemonitoring ->",
      "EXISTS" if r.json().get("value") else "missing")
