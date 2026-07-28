#!/usr/bin/env python3
"""Report alternate-key index status on new_engagementsignal (must be Active
before the first ingestion run — index builds asynchronously after creation)."""
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provision import load_env, get_token  # noqa: E402

env = load_env(Path(__file__).resolve().parent.parent / ".env")
url = env["DATAVERSE_URL"].rstrip("/")
token = get_token(env, url + "/")
r = requests.get(
    url + "/api/data/v9.2/EntityDefinitions(LogicalName='new_engagementsignal')/Keys"
          "?$select=SchemaName,EntityKeyIndexStatus",
    headers={"Authorization": "Bearer " + token}, timeout=60,
)
r.raise_for_status()
for k in r.json()["value"]:
    print(f"{k['SchemaName']} -> {k['EntityKeyIndexStatus']}")
