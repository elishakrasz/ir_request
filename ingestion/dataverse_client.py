"""Dataverse Web API client: auth, scope queries, existence checks, writes.

Writes honor dry-run (spec house rule 4): with apply=False every write is
recorded as intent and nothing is sent. Sequential writes for v1 — documented
deviation from the spec's "$batch ≤100" (volume at 8 mailboxes is small;
revisit if the initial backfill proves slow).
"""
import time

import requests


class DataverseClient:
    def __init__(self, url: str, tenant_id: str, client_id: str, client_secret: str,
                 prefix: str, apply: bool):
        self.url = url.rstrip("/")
        self.base = self.url + "/api/data/v9.2"
        self.p = prefix
        self.apply = apply
        self.intents: list[str] = []      # dry-run log
        self.created = 0
        self.patched = 0
        self._tenant, self._cid, self._secret = tenant_id, client_id, client_secret
        self._token, self._exp = None, 0.0
        self.s = requests.Session()
        self.s.headers.update({"OData-MaxVersion": "4.0", "OData-Version": "4.0",
                               "Accept": "application/json",
                               "Content-Type": "application/json"})

    # ── auth / transport ─────────────────────────────────────────────────────
    def _tok(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        r = requests.post(
            f"https://login.microsoftonline.com/{self._tenant}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials", "client_id": self._cid,
                  "client_secret": self._secret, "scope": f"{self.url}/.default"},
            timeout=30)
        r.raise_for_status()
        j = r.json()
        self._token, self._exp = j["access_token"], time.time() + int(j["expires_in"])
        return self._token

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        for attempt in range(6):
            r = self.s.request(method, f"{self.base}/{path}", timeout=120,
                               headers={"Authorization": f"Bearer {self._tok()}",
                                        **kw.pop("headers", {})}, **kw)
            if r.status_code == 429:
                wait = min(int(r.headers.get("Retry-After", 2 ** attempt)), 120)
                print(f"[dv] 429 — waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
            return r
        return r

    def query(self, path: str) -> list[dict]:
        """GET a collection, following @odata.nextLink."""
        rows, url = [], path
        prefer = 'odata.maxpagesize=5000,odata.include-annotations="*"'
        while url:
            r = self._req("GET", url, headers={"Prefer": prefer})
            r.raise_for_status()
            j = r.json()
            rows.extend(j.get("value", []))
            nxt = j.get("@odata.nextLink")
            url = nxt[len(self.base) + 1:] if nxt else None
        return rows

    # ── scope reads (spec step 1) ────────────────────────────────────────────
    def fetch_opportunities(self, fund_lookup: str = "mint_fundorspv") -> list[dict]:
        p = self.p
        return self.query(
            "opportunities?$select=opportunityid,name,new_live,new_prospectcode,"
            f"{p}oppcode,{p}aliases,{p}activemonitoring,{p}monitoringstartdate,"
            f"_parentcontactid_value,_customerid_value,_{fund_lookup}_value"
            "&$filter=statecode eq 0")

    def fetch_regarding_map(self, since_iso: str) -> dict:
        """WS1 booster 1: {internetMessageId → opportunityid} from email
        activities whose Regarding is an Opportunity (server-side sync /
        Dynamics App for Outlook ground truth)."""
        rows = self.query(
            "emails?$select=messageid,_regardingobjectid_value"
            f"&$filter=createdon ge {since_iso} and _regardingobjectid_value ne null")
        out = {}
        for r in rows:
            if r.get("_regardingobjectid_value@Microsoft.Dynamics.CRM.lookuplogicalname") \
                    == "opportunity" and r.get("messageid"):
                out[r["messageid"]] = r["_regardingobjectid_value"]
        return out

    def fetch_connections(self) -> list[dict]:
        # contact objecttypecode 2, opportunity 3; live connections only
        return self.query(
            "connections?$select=_record1id_value,_record2id_value,"
            "record1objecttypecode,record2objecttypecode,_record1roleid_value"
            "&$filter=statecode eq 0 and "
            "((record1objecttypecode eq 2 and record2objecttypecode eq 3) or "
            "(record1objecttypecode eq 3 and record2objecttypecode eq 2))")

    def fetch_connection_roles(self) -> list[dict]:
        return self.query("connectionroles?$select=connectionroleid,name")

    def fetch_contacts(self, contact_ids: list[str]) -> list[dict]:
        rows = []
        ids = list(contact_ids)
        for i in range(0, len(ids), 20):
            flt = " or ".join(f"contactid eq {c}" for c in ids[i:i + 20])
            rows.extend(self.query(
                "contacts?$select=contactid,fullname,emailaddress1,emailaddress2,"
                f"emailaddress3&$filter={flt}"))
        return rows

    def has_attribute(self, entity_logical: str, attr_logical: str) -> bool:
        """Metadata probe, cached per run. Lets sync write columns that exist in
        DEV but haven't reached PROD via manual solution import yet (the same
        pattern as §0.1 close-readiness columns) without failing creates."""
        cache = getattr(self, "_attr_cache", None)
        if cache is None:
            cache = self._attr_cache = {}
        key = (entity_logical, attr_logical)
        if key not in cache:
            r = self._req(
                "GET",
                f"EntityDefinitions(LogicalName='{entity_logical}')/Attributes"
                f"?$select=LogicalName&$filter=LogicalName eq '{attr_logical}'")
            cache[key] = bool(r.status_code == 200 and r.json().get("value"))
        return cache[key]

    def category_option_values(self, entity_logical: str, attr_logical: str) -> set | None:
        """Set of option VALUES for a picklist (cached per run); None if the probe
        fails. Lets sync fold a category the target env's option set doesn't have
        yet to a safe fallback instead of 400-ing on an unknown option value —
        the same 'code may precede schema' pattern as has_attribute, but for
        newly-appended options (which add no column to probe)."""
        cache = getattr(self, "_optset_cache", None)
        if cache is None:
            cache = self._optset_cache = {}
        key = (entity_logical, attr_logical)
        if key not in cache:
            vals = None
            r = self._req(
                "GET",
                f"EntityDefinitions(LogicalName='{entity_logical}')/Attributes/"
                "Microsoft.Dynamics.CRM.PicklistAttributeMetadata"
                f"?$select=LogicalName&$filter=LogicalName eq '{attr_logical}'"
                "&$expand=OptionSet($select=Options)")
            if r.status_code == 200 and r.json().get("value"):
                try:
                    opts = r.json()["value"][0]["OptionSet"]["Options"]
                    vals = {o["Value"] for o in opts}
                except (KeyError, IndexError, TypeError):
                    vals = None
            cache[key] = vals
        return cache[key]

    def find_contact_by_email(self, email: str) -> dict | None:
        """ANY contact holding this address — the scope map only covers
        opportunity-linked contacts, so intake reuse-before-create must ask
        Dataverse directly (docs/ir-intake-design.md)."""
        e = email.replace("'", "''")
        rows = self.query(
            "contacts?$select=contactid,fullname&$filter="
            f"emailaddress1 eq '{e}' or emailaddress2 eq '{e}' "
            f"or emailaddress3 eq '{e}'")
        return rows[0] if rows else None

    # ── signal reads ─────────────────────────────────────────────────────────
    def get_signal(self, keyhash: str, contact_id: str) -> dict | None:
        p = self.p
        rows = self.query(
            f"{p}engagementsignals?$filter={p}messagekeyhash eq '{keyhash}' "
            f"and _{p}contact_value eq {contact_id}")
        return rows[0] if rows else None

    def confirmed_conv_opps(self, conv_ids: list[str], confirmed_value: int) -> dict:
        """{conversationId: opportunityid} from already-Confirmed signals."""
        p, out = self.p, {}
        ids = [c for c in set(conv_ids) if c]
        for i in range(0, len(ids), 20):
            flt = " or ".join(f"{p}conversationid eq '{c}'" for c in ids[i:i + 20])
            rows = self.query(
                f"{p}engagementsignals?$select={p}conversationid,"
                f"_{p}opportunity_value&$filter=({flt}) and "
                f"{p}matchstatus eq {confirmed_value} and "
                f"_{p}opportunity_value ne null")
            for r in rows:
                out.setdefault(r[f"{p}conversationid"], r[f"_{p}opportunity_value"])
        return out

    def conversation_signals(self, conv_id: str) -> list[dict]:
        p = self.p
        return self.query(
            f"{p}engagementsignals?$select={p}direction,{p}timestamputc,"
            f"{p}responselatencymin,{p}rfistatus,{p}engagementsignalid,"
            f"{p}ismeaningful,{p}matchstatus,_{p}contact_value"
            f"&$filter={p}conversationid eq '{conv_id}'")

    def requests_for_signals(self, signal_ids: list[str], open_values: list[int]) -> list[dict]:
        p, rows = self.p, []
        for i in range(0, len(signal_ids), 20):
            flt = " or ".join(f"_{p}sourcesignal_value eq {s}" for s in signal_ids[i:i + 20])
            status_flt = " or ".join(f"{p}status eq {v}" for v in open_values)
            rows.extend(self.query(
                f"{p}inforequests?$select={p}inforequestid,{p}status,"
                f"_{p}sourcesignal_value&$filter=({flt}) and ({status_flt})"))
        return rows

    # ── writes (dry-run aware) ───────────────────────────────────────────────
    def create(self, entity_set: str, payload: dict, describe: str) -> dict | None:
        """Returns the created row (return=representation) in apply mode."""
        self.intents.append(f"CREATE {describe}")
        if not self.apply:
            return None
        r = self._req("POST", entity_set, json=payload,
                      headers={"Prefer": "return=representation"})
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"create failed ({describe}): {r.status_code} {r.text[:500]}")
        self.created += 1
        return r.json() if r.content else {}

    def patch(self, entity_set: str, row_id: str, payload: dict, describe: str):
        self.intents.append(f"PATCH {describe}")
        if not self.apply:
            return
        r = self._req("PATCH", f"{entity_set}({row_id})", json=payload,
                      headers={"If-Match": "*"})   # update-only, never upsert-create
        if r.status_code not in (200, 204):
            raise RuntimeError(f"patch failed ({describe}): {r.status_code} {r.text[:500]}")
        self.patched += 1
