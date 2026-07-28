"""Microsoft Graph client: client-credentials auth, folder delta queries with
resync handling, 429/5xx retry with backoff, enrichment GET (spec steps 3+6).
"""
import time

import requests

GRAPH = "https://graph.microsoft.com/v1.0"

# Delta-level metadata only. internetMessageHeaders is NOT available on
# delta/list queries — header checks happen in enrich(). bccRecipients matters
# on SentItems (outbound mail whose only external recipients are BCC'd).
DELTA_SELECT = ("id,internetMessageId,conversationId,subject,bodyPreview,from,"
                "toRecipients,ccRecipients,bccRecipients,sentDateTime,"
                "receivedDateTime,webLink")


class GraphClient:
    def __init__(self, tenant_id: str, client_id: str, client_secret: str):
        self._tenant, self._cid, self._secret = tenant_id, client_id, client_secret
        self._token, self._exp = None, 0.0
        self.s = requests.Session()

    def clone(self) -> "GraphClient":
        """Fresh client (own Session + token) for a parallel walk worker."""
        return GraphClient(self._tenant, self._cid, self._secret)

    def _tok(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        r = requests.post(
            f"https://login.microsoftonline.com/{self._tenant}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials", "client_id": self._cid,
                  "client_secret": self._secret,
                  "scope": "https://graph.microsoft.com/.default"},
            timeout=30)
        r.raise_for_status()
        j = r.json()
        self._token, self._exp = j["access_token"], time.time() + int(j["expires_in"])
        return self._token

    def _get(self, url: str, headers: dict | None = None) -> requests.Response:
        h = {"Authorization": f"Bearer {self._tok()}"}
        if headers:
            h.update(headers)
        for attempt in range(6):
            r = self.s.get(url, headers=h, timeout=120)
            if r.status_code in (429, 503, 504):
                wait = min(int(r.headers.get("Retry-After", 2 ** attempt)), 120)
                print(f"[graph] {r.status_code} — waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
            if r.status_code == 401 and attempt == 0:  # token edge-expiry
                self._token = None
                h["Authorization"] = f"Bearer {self._tok()}"
                continue
            return r
        return r

    def delta_messages(self, mailbox: str, folder: str, delta_link: str | None = None):
        """Walk a folder delta. Returns (messages, new_delta_link, resynced).
        @removed entries are dropped (a deleted message doesn't un-happen as
        engagement). Handles resyncRequired (410) by restarting fresh — safe
        because upserts are idempotent."""
        fresh = (f"{GRAPH}/users/{mailbox}/mailFolders/{folder}/messages/delta"
                 f"?$select={DELTA_SELECT}")
        url, resynced, msgs = delta_link or fresh, False, []
        while True:
            r = self._get(url, headers={"Prefer": "odata.maxpagesize=200"})
            if r.status_code == 410:  # resyncRequired
                url, resynced, msgs = r.headers.get("Location", fresh), True, []
                continue
            r.raise_for_status()
            j = r.json()
            msgs.extend(m for m in j.get("value", []) if "@removed" not in m)
            if "@odata.nextLink" in j:
                url = j["@odata.nextLink"]
                continue
            return msgs, j.get("@odata.deltaLink"), resynced

    def enrich(self, mailbox: str, msg_id: str) -> dict:
        """Single-message GET for matched messages only: real headers to finalize
        ismeaningful + plain-text body for the classifier (in memory only)."""
        r = self._get(
            f"{GRAPH}/users/{mailbox}/messages/{msg_id}"
            "?$select=internetMessageHeaders,uniqueBody,webLink",
            headers={"Prefer": 'outlook.body-content-type="text"'})
        if r.status_code != 200:
            return {"headers": [], "body": ""}
        j = r.json()
        return {"headers": j.get("internetMessageHeaders", []),
                "body": (j.get("uniqueBody") or {}).get("content", "") or ""}
