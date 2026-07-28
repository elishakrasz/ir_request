#!/usr/bin/env python3
"""Verify Graph Mail.Read access for every configured mailbox (read-only).

Positive: every mailbox in MAILBOXES must be readable (Inbox folder GET).
Out-of-scope probe: informational only — the operator ACCEPTED tenant-wide
Mail.Read on the shared app (2026-07-28; other workloads need it). Mailbox
scope for this system is enforced solely by the MAILBOXES allowlist.

Usage: python -m ingestion.verify_scope [--negative someone@domain]
"""
import argparse
import sys
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "solution"))
from provision import load_env, get_token  # noqa: E402

GRAPH = "https://graph.microsoft.com/v1.0"


def probe(token: str, mailbox: str):
    r = requests.get(
        f"{GRAPH}/users/{mailbox}/mailFolders/Inbox?$select=id,displayName,totalItemCount",
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    return r.status_code, (r.json().get("error", {}).get("code") if r.status_code >= 400
                           else r.json().get("totalItemCount"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--negative", default="emallard@exigentcap.com",
                    help="mailbox expected to be OUT of scope (default: emallard@)")
    args = ap.parse_args()

    env = load_env(REPO / ".env")
    token = get_token(env, "https://graph.microsoft.com/")
    ok = True

    print("— in-scope mailboxes (expect 200):")
    for mb in env["MAILBOXES"].split(","):
        mb = mb.strip()
        code, detail = probe(token, mb)
        mark = "✓" if code == 200 else "✗"
        ok &= code == 200
        print(f"  {mark} {mb}: {code}"
              + (f" (Inbox items: {detail})" if code == 200 else f" ({detail})"))

    print(f"— out-of-scope probe {args.negative} (informational):")
    code, detail = probe(token, args.negative)
    if code == 200:
        print("  ⚠ accessible — tenant-wide Mail.Read (operator-accepted 2026-07-28; "
              "scope control is the MAILBOXES allowlist, not authorization)")
    else:
        print(f"  scoped: {code} ({detail})")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
