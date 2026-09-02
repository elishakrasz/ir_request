"""One-off READ-ONLY pull: incoming mail to ir@exigentcap.com since 2024-08-04.

Metadata + bodyPreview only (house rule 6 — no full bodies). Output lands in
reports/ (gitignored, PII). Used to derive a triage category taxonomy.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ingestion.graph_client import GraphClient, GRAPH  # noqa: E402

MAILBOX = "ir@exigentcap.com"
SINCE = "2024-08-04T00:00:00Z"
OUT = Path(__file__).parent / "reports" / "ir_inbox_raw.jsonl"

SELECT = ("id,parentFolderId,subject,bodyPreview,from,toRecipients,ccRecipients,"
          "sentDateTime,receivedDateTime,conversationId,internetMessageId")


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def folder_map(gc: GraphClient) -> dict:
    """id -> full folder path, walking the whole tree."""
    fmap = {}

    def walk(url, prefix):
        while url:
            r = gc._get(url)
            r.raise_for_status()
            j = r.json()
            for f in j.get("value", []):
                path = (prefix + "/" if prefix else "") + f["displayName"]
                fmap[f["id"]] = path
                if f.get("childFolderCount", 0) > 0:
                    walk(f"{GRAPH}/users/{MAILBOX}/mailFolders/{f['id']}"
                         "/childFolders?$top=100", path)
            url = j.get("@odata.nextLink")

    walk(f"{GRAPH}/users/{MAILBOX}/mailFolders?$top=100", "")
    return fmap


def main():
    env = load_env(Path(__file__).parent / ".env")
    gc = GraphClient(env["TENANT_ID"], env["CLIENT_ID"], env["CLIENT_SECRET"])

    print("mapping folders...", flush=True)
    fmap = folder_map(gc)
    print(f"  {len(fmap)} folders: {sorted(fmap.values())}", flush=True)

    url = (f"{GRAPH}/users/{MAILBOX}/messages"
           f"?$filter=receivedDateTime ge {SINCE}"
           f"&$select={SELECT}&$orderby=receivedDateTime desc&$top=100")
    n = kept = 0
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        while url:
            r = gc._get(url)
            r.raise_for_status()
            j = r.json()
            for m in j.get("value", []):
                n += 1
                sender = ((m.get("from") or {}).get("emailAddress") or {})
                addr = (sender.get("address") or "").lower()
                if addr.endswith("@exigentcap.com"):
                    continue  # outbound/internal — not incoming correspondence
                fh.write(json.dumps({
                    "folder": fmap.get(m.get("parentFolderId"), "?"),
                    "received": m.get("receivedDateTime"),
                    "from_addr": addr,
                    "from_name": sender.get("name") or "",
                    "subject": m.get("subject") or "",
                    "preview": (m.get("bodyPreview") or "")[:255],
                    "conversationId": m.get("conversationId"),
                    "internetMessageId": m.get("internetMessageId"),
                    "to": [((x.get("emailAddress") or {}).get("address") or "").lower()
                           for x in (m.get("toRecipients") or [])],
                }, ensure_ascii=False) + "\n")
                kept += 1
            if n % 1000 < 100:
                print(f"  scanned {n}, kept {kept}", flush=True)
            url = j.get("@odata.nextLink")
    print(f"done: scanned {n} messages, kept {kept} inbound -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
