"""Apply-run progress: signal count + write rate (read-only).

Usage: venv/bin/python -m ingestion.progress
"""
from datetime import datetime, timezone

import requests

from .config import Config

cfg = Config.from_env()
r = requests.post(
    f"https://login.microsoftonline.com/{cfg.tenant_id}/oauth2/v2.0/token",
    data={"grant_type": "client_credentials", "client_id": cfg.client_id,
          "client_secret": cfg.client_secret,
          "scope": f"{cfg.dataverse_url}/.default"}, timeout=30)
h = {"Authorization": "Bearer " + r.json()["access_token"]}
api = cfg.dataverse_url + "/api/data/v9.2"
p = cfg.prefix

n = int(requests.get(f"{api}/{p}engagementsignals/$count", headers=h,
                     timeout=60).text.lstrip("﻿"))
rows = requests.get(f"{api}/{p}engagementsignals?$select=createdon"
                    "&$orderby=createdon asc&$top=1", headers=h,
                    timeout=60).json()["value"]
if not rows:
    print(f"signals: {n} — writes not started (still in the delta walk)")
else:
    first = datetime.fromisoformat(rows[0]["createdon"].replace("Z", "+00:00"))
    mins = (datetime.now(timezone.utc) - first).total_seconds() / 60
    rate = n / max(mins, 0.1)
    print(f"signals: {n}/~2830 in {mins:.0f} min ({rate:.0f}/min)"
          + (f" — est. ~{(2830 - n) / rate:.0f} min left for creates"
             if 0 < n < 2830 else ""))
