#!/usr/bin/env python3
"""Provision the EngagementDashboard solution schema in the DEV Dataverse environment.

Idempotent: every component is checked before creation; re-running against a fully
provisioned environment performs zero writes. Dry-run is the DEFAULT — pass --apply
to write (house rule 4: first execution against any environment is always a dry run).

House rule 1 guard: refuses to run against any URL containing 'exigentcrmprod'.
PROD only ever receives this schema via manual maker-portal solution import.

Auth: app-only client credentials from ../.env. The DEV application user must hold
System Customizer (schema creation) — see docs/phase0-checklist.md section 3.

Usage:
    python solution/provision.py             # dry run (reads metadata, lists intent)
    python solution/provision.py --apply     # perform the creates

What it creates (all inside solution 'EngagementDashboard'):
  - publisher (reuses any existing publisher with prefix 'new'; else creates one)
  - table new_engagementsignal (org-owned) + all columns
  - table new_inforequest (user-owned; ownerid IS the owner field) + all columns
  - lookups (relationships) to opportunity / contact / signal
  - alternate key (new_messagekeyhash, new_contact) on the signal table
  - opportunity extension columns (oppcode, aliases, activemonitoring,
    monitoringstartdate, teamschannelid)

NOT created here (see docs/schema.md):
  - the 6 rollup columns on contact/opportunity — maker-portal step (rollup
    definitions are workflow XAML; not sanely authorable via API or solution XML)
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
SOLUTION = "EngagementDashboard"
API = "api/data/v9.2"


# ── env / auth ────────────────────────────────────────────────────────────────

def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def get_token(env: dict, resource: str) -> str:
    r = requests.post(
        f"https://login.microsoftonline.com/{env['TENANT_ID']}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": env["CLIENT_ID"],
            "client_secret": env["CLIENT_SECRET"],
            "scope": f"{resource}.default",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


class Api:
    """Thin Dataverse Web API wrapper with 429 retry and solution-scoped writes."""

    def __init__(self, base_url: str, token: str, apply: bool):
        self.base = base_url.rstrip("/") + "/" + API
        self.apply = apply
        self.creates = []  # dry-run intent log
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _req(self, method, path, **kw):
        for attempt in range(5):
            r = self.s.request(method, f"{self.base}/{path}", timeout=120, **kw)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 2 ** attempt))
                print(f"    429 — waiting {wait}s")
                time.sleep(wait)
                continue
            return r
        r.raise_for_status()

    def get(self, path):
        r = self._req("GET", path)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def create(self, path, body, describe, in_solution=True):
        """Create a component (solution-scoped). In dry-run, only record intent."""
        self.creates.append(describe)
        if not self.apply:
            print(f"  DRY-RUN would create: {describe}")
            return
        # publisher/solution rows are created BEFORE the solution exists — no header
        h = {"MSCRM.SolutionUniqueName": SOLUTION} if in_solution else {}
        r = self._req("POST", path, json=body, headers=h)
        if r.status_code not in (200, 201, 204):
            print(f"  FAILED: {describe}\n    {r.status_code}: {r.text[:2000]}")
            sys.exit(2)
        print(f"  created: {describe}")


# ── metadata payload helpers ──────────────────────────────────────────────────

def label(text: str) -> dict:
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.Label",
        "LocalizedLabels": [{
            "@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
            "Label": text, "LanguageCode": 1033,
        }],
    }


def string_attr(schema, display, maxlen, fmt="Text", desc=None):
    a = {
        "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
        "SchemaName": schema, "DisplayName": label(display),
        "MaxLength": maxlen, "FormatName": {"Value": fmt},
    }
    if desc:
        a["Description"] = label(desc)
    return a


def memo_attr(schema, display, maxlen, desc=None):
    a = {
        "@odata.type": "Microsoft.Dynamics.CRM.MemoAttributeMetadata",
        "SchemaName": schema, "DisplayName": label(display), "MaxLength": maxlen,
    }
    if desc:
        a["Description"] = label(desc)
    return a


def bool_attr(schema, display, default=False, desc=None):
    a = {
        "@odata.type": "Microsoft.Dynamics.CRM.BooleanAttributeMetadata",
        "SchemaName": schema, "DisplayName": label(display),
        "DefaultValue": default,
        "OptionSet": {
            "@odata.type": "Microsoft.Dynamics.CRM.BooleanOptionSetMetadata",
            "TrueOption": {"Value": 1, "Label": label("Yes")},
            "FalseOption": {"Value": 0, "Label": label("No")},
        },
    }
    if desc:
        a["Description"] = label(desc)
    return a


def int_attr(schema, display, minv=None, maxv=None, desc=None):
    a = {
        "@odata.type": "Microsoft.Dynamics.CRM.IntegerAttributeMetadata",
        "SchemaName": schema, "DisplayName": label(display),
    }
    if minv is not None:
        a["MinValue"] = minv
    if maxv is not None:
        a["MaxValue"] = maxv
    if desc:
        a["Description"] = label(desc)
    return a


def datetime_attr(schema, display, date_only=False, desc=None):
    a = {
        "@odata.type": "Microsoft.Dynamics.CRM.DateTimeAttributeMetadata",
        "SchemaName": schema, "DisplayName": label(display),
        "Format": "DateOnly" if date_only else "DateAndTime",
        # UserLocal = stored as UTC, rendered in the viewer's timezone.
        "DateTimeBehavior": {"Value": "DateOnly" if date_only else "UserLocal"},
    }
    if desc:
        a["Description"] = label(desc)
    return a


def picklist_attr(schema, display, options, value_base, default_index=None, desc=None):
    """options: list of option label strings; values are value_base + index."""
    a = {
        "@odata.type": "Microsoft.Dynamics.CRM.PicklistAttributeMetadata",
        "SchemaName": schema, "DisplayName": label(display),
        "OptionSet": {
            "@odata.type": "Microsoft.Dynamics.CRM.OptionSetMetadata",
            "IsGlobal": False, "OptionSetType": "Picklist",
            "Options": [
                {"Value": value_base + i, "Label": label(t)}
                for i, t in enumerate(options)
            ],
        },
    }
    if default_index is not None:
        a["DefaultFormValue"] = value_base + default_index
    if desc:
        a["Description"] = label(desc)
    return a


# ── idempotent ensure-helpers ─────────────────────────────────────────────────

def ensure_entity(api, schema, display, plural, org_owned, primary_desc):
    logical = schema.lower()
    if api.get(f"EntityDefinitions(LogicalName='{logical}')"):
        print(f"  exists: table {logical}")
        return
    body = {
        "@odata.type": "Microsoft.Dynamics.CRM.EntityMetadata",
        "SchemaName": schema,
        "DisplayName": label(display),
        "DisplayCollectionName": label(plural),
        "OwnershipType": "OrganizationOwned" if org_owned else "UserOwned",
        "HasNotes": False, "HasActivities": False, "IsActivity": False,
        "Attributes": [{
            **string_attr(schema.rsplit("_", 1)[0] + "_name", "Name", 200, desc=primary_desc),
            "IsPrimaryName": True,
        }],
    }
    api.create("EntityDefinitions", body, f"table {logical} ({display})")


def ensure_attribute(api, entity_logical, attr):
    logical = attr["SchemaName"].lower()
    existing = api.get(
        f"EntityDefinitions(LogicalName='{entity_logical}')/Attributes"
        f"?$select=LogicalName&$filter=LogicalName eq '{logical}'"
    )
    if existing and existing.get("value"):
        print(f"  exists: {entity_logical}.{logical}")
        return
    api.create(
        f"EntityDefinitions(LogicalName='{entity_logical}')/Attributes",
        attr, f"column {entity_logical}.{logical}",
    )


def ensure_lookup(api, rel_schema, referenced, referencing, lookup_schema, display):
    existing = api.get(
        "RelationshipDefinitions/Microsoft.Dynamics.CRM.OneToManyRelationshipMetadata"
        f"?$select=SchemaName&$filter=SchemaName eq '{rel_schema}'"
    )
    if existing and existing.get("value"):
        print(f"  exists: lookup {referencing}.{lookup_schema.lower()}")
        return
    body = {
        "@odata.type": "Microsoft.Dynamics.CRM.OneToManyRelationshipMetadata",
        "SchemaName": rel_schema,
        "ReferencedEntity": referenced,
        "ReferencingEntity": referencing,
        "CascadeConfiguration": {
            "Assign": "NoCascade", "Delete": "RemoveLink", "Merge": "NoCascade",
            "Reparent": "NoCascade", "Share": "NoCascade", "Unshare": "NoCascade",
        },
        "AssociatedMenuConfiguration": {
            "Behavior": "UseCollectionName", "Group": "Details", "Order": 10000,
        },
        "Lookup": {
            "@odata.type": "Microsoft.Dynamics.CRM.LookupAttributeMetadata",
            "SchemaName": lookup_schema, "DisplayName": label(display),
        },
    }
    api.create("RelationshipDefinitions", body,
               f"lookup {referencing}.{lookup_schema.lower()} → {referenced}")


def ensure_key(api, entity_logical, key_schema, display, attrs):
    existing = api.get(
        f"EntityDefinitions(LogicalName='{entity_logical}')/Keys?$select=SchemaName"
    )
    if existing and any(k["SchemaName"].lower() == key_schema.lower()
                        for k in existing.get("value", [])):
        print(f"  exists: key {key_schema}")
        return
    api.create(
        f"EntityDefinitions(LogicalName='{entity_logical}')/Keys",
        {"SchemaName": key_schema, "DisplayName": label(display), "KeyAttributes": attrs},
        f"alternate key {entity_logical}({', '.join(attrs)})",
    )


# ── main ──────────────────────────────────────────────────────────────────────

def ensure_option(api, entity_logical, attr_logical, label_text, value):
    """Append an option to a local option set (append-only — never reorder)."""
    r = api._req("GET", f"EntityDefinitions(LogicalName='{entity_logical}')"
                        f"/Attributes(LogicalName='{attr_logical}')"
                        "/Microsoft.Dynamics.CRM.PicklistAttributeMetadata"
                        "?$expand=OptionSet($select=Options)")
    r.raise_for_status()
    existing = {o["Value"] for o in r.json()["OptionSet"]["Options"]}
    if value in existing:
        print(f"  exists: option {attr_logical}={label_text}")
        return
    api.create("InsertOptionValue", {
        "EntityLogicalName": entity_logical,
        "AttributeLogicalName": attr_logical,
        "Value": value,
        "Label": label(label_text),
    }, f"option {entity_logical}.{attr_logical} += {label_text}({value})")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="perform writes (default is dry-run)")
    args = ap.parse_args()

    env = load_env(ENV_PATH)
    import os
    if os.environ.get("PROVISION_URL"):   # point at DEV while .env holds PROD
        env["DATAVERSE_URL"] = os.environ["PROVISION_URL"]
    url = env.get("DATAVERSE_URL", "")
    if not url:
        sys.exit("DATAVERSE_URL is empty in .env")
    if "exigentcrmprod" in url.lower():
        sys.exit("REFUSING: DATAVERSE_URL points at PROD (house rule 1). "
                 "PROD is only ever touched by manual maker-portal solution import.")
    p = env.get("PREFIX", "new_").rstrip("_") + "_"  # e.g. 'new_'

    print(f"{'APPLY' if args.apply else 'DRY RUN'} against {url} (solution {SOLUTION}, prefix {p})")
    token = get_token(env, url if url.endswith("/") else url + "/")
    api = Api(url, token, args.apply)

    # 1. publisher — reuse any publisher already owning this prefix
    pub = api.get(f"publishers?$select=publisherid,uniquename,customizationoptionvalueprefix"
                  f"&$filter=customizationprefix eq '{p.rstrip('_')}'")
    if pub and pub.get("value"):
        pub_row = pub["value"][0]
        print(f"  exists: publisher '{pub_row['uniquename']}' (prefix {p})")
        value_base = pub_row["customizationoptionvalueprefix"] * 10000
        pub_id = pub_row["publisherid"]
    else:
        value_base = 65100 * 10000
        pub_id = None
        api.create("publishers", {
            "uniquename": "exigentengagement",
            "friendlyname": "Exigent Engagement",
            "customizationprefix": p.rstrip("_"),
            "customizationoptionvalueprefix": 65100,
        }, f"publisher exigentengagement (prefix {p})", in_solution=False)
        if args.apply:
            pub = api.get("publishers?$select=publisherid"
                          "&$filter=uniquename eq 'exigentengagement'")
            pub_id = pub["value"][0]["publisherid"]

    # 2. solution
    sol = api.get(f"solutions?$select=solutionid&$filter=uniquename eq '{SOLUTION}'")
    if sol and sol.get("value"):
        print(f"  exists: solution {SOLUTION}")
    else:
        body = {"uniquename": SOLUTION, "friendlyname": "Engagement Dashboard",
                "version": "1.0.0.0"}
        if pub_id:
            body["publisherid@odata.bind"] = f"/publishers({pub_id})"
        api.create("solutions", body, f"solution {SOLUTION}", in_solution=False)

    sig = f"{p}engagementsignal"
    req = f"{p}inforequest"

    # 3. tables
    print("— tables")
    ensure_entity(api, sig, "Engagement Signal", "Engagement Signals",
                  org_owned=True, primary_desc="Subject, truncated to 200")
    ensure_entity(api, req, "Information Request", "Information Requests",
                  org_owned=False, primary_desc="Request title")

    # 4. signal columns
    print("— engagement signal columns")
    vb = value_base
    for a in [
        picklist_attr(f"{p}channel", "Channel", ["Email", "Teams"], vb),
        picklist_attr(f"{p}direction", "Direction",
                      ["Inbound", "Outbound", "Internal"], vb),
        datetime_attr(f"{p}timestamputc", "Timestamp (UTC)",
                      desc="Message sent/received time; ingestion always writes UTC"),
        string_attr(f"{p}sender", "Sender", 320, desc="SMTP address"),
        memo_attr(f"{p}participants", "Participants", 4000,
                  desc="JSON array of addresses"),
        memo_attr(f"{p}snippet", "Snippet", 2000, desc="Body preview only — never the full body"),
        string_attr(f"{p}conversationid", "Conversation Id", 512),
        string_attr(f"{p}messagekey", "Message Key", 512,
                    desc="internetMessageId (email) / message id (Teams) — informational"),
        string_attr(f"{p}messagekeyhash", "Message Key Hash", 64,
                    desc="SHA-256 hex of messagekey — the alternate-key column"),
        bool_attr(f"{p}isinforequest", "Is Info Request",
                  desc="Set by classifier"),
        picklist_attr(f"{p}rfistatus", "RFI Status",
                      ["NA", "Open", "Answered", "Overdue"], vb, default_index=0,
                      desc="Overdue is reserved — overdue-ness is derived from duedate in the app layer"),
        int_attr(f"{p}responselatencymin", "Response Latency (min)", minv=0),
        int_attr(f"{p}matchconfidence", "Match Confidence", minv=0, maxv=100),
        picklist_attr(f"{p}matchmethod", "Match Method",
                      ["Explicit", "Thread", "ContactMatch", "Content", "Manual"], vb),
        picklist_attr(f"{p}matchstatus", "Match Status",
                      ["Confirmed", "Suggested", "Unmatched", "Excluded"], vb),
        string_attr(f"{p}sourcelink", "Source Link", 2000, fmt="Url",
                    desc="Graph webLink — opens only in the synced mailbox"),
        bool_attr(f"{p}ismeaningful", "Is Meaningful", default=True,
                  desc="False for auto-replies/bounces/mass mail; all KPIs filter on true"),
        string_attr(f"{p}provenance", "Provenance", 512,
                    desc="mailbox|runid|codeversion"),
        string_attr(f"{p}modifiedbyhint", "Modified By (hint)", 200,
                    desc="Human identity behind dashboard-initiated writes"),
    ]:
        ensure_attribute(api, sig, a)

    # 5. info request columns
    print("— information request columns")
    for a in [
        picklist_attr(f"{p}category", "Category",
                      ["Reporting", "CapitalAccount", "Valuation", "KYC-AML",
                       "SubscriptionDocs", "Legal-SideLetter", "Meeting",
                       "DataRoom", "Other"], vb),
        datetime_attr(f"{p}receiveddate", "Received"),
        datetime_attr(f"{p}duedate", "Due",
                      desc="Overdue-ness is derived from this in the app layer"),
        datetime_attr(f"{p}completeddate", "Completed"),
        picklist_attr(f"{p}status", "Status",
                      ["New", "InProgress", "WaitingInternal", "WaitingExternal",
                       "Completed", "Cancelled"], vb, default_index=0),
        int_attr(f"{p}firstresponseminutes", "First Response (min)", minv=0),
        bool_attr(f"{p}aigenerated", "AI Generated",
                  desc="Set by classifier; human corrections set Human Confirmed"),
        bool_attr(f"{p}humanconfirmed", "Human Confirmed"),
        string_attr(f"{p}modifiedbyhint", "Modified By (hint)", 200),
    ]:
        ensure_attribute(api, req, a)
    # note: owner field = ownerid (table is user-owned); no custom owner column

    # 6. lookups
    print("— lookups")
    ensure_lookup(api, f"{p}opportunity_engagementsignal", "opportunity", sig,
                  f"{p}opportunity", "Opportunity")
    ensure_lookup(api, f"{p}contact_engagementsignal", "contact", sig,
                  f"{p}contact", "Contact")
    ensure_lookup(api, f"{p}opportunity_inforequest", "opportunity", req,
                  f"{p}opportunity", "Opportunity")
    ensure_lookup(api, f"{p}contact_inforequest", "contact", req,
                  f"{p}contact", "Contact")
    ensure_lookup(api, f"{p}signal_inforequest", sig, req,
                  f"{p}sourcesignal", "Source Signal")

    # 7. alternate key (after lookup exists)
    print("— alternate key")
    ensure_key(api, sig, f"{p}signalidentity",
               "Signal identity (messagekeyhash + contact)",
               [f"{p}messagekeyhash", f"{p}contact"])

    # 8. opportunity extension columns
    print("— opportunity extension columns")
    for a in [
        string_attr(f"{p}oppcode", "Opp Code", 20,
                    desc="Short unique code (e.g. OPP-0042) for explicit matching in subjects"),
        memo_attr(f"{p}aliases", "Aliases", 4000,
                  desc="Fund names, project names, abbreviations — matching evidence"),
        bool_attr(f"{p}activemonitoring", "Active Monitoring"),
        datetime_attr(f"{p}monitoringstartdate", "Monitoring Start", date_only=True,
                      desc="Ignore mail sent before this date"),
        string_attr(f"{p}teamschannelid", "Teams Channel Id", 200,
                    desc="Reserved for Phase 5"),
    ]:
        ensure_attribute(api, "opportunity", a)

    # 9. v2 revision columns (WS1 noise gate, WS5 summaries, WS6 urgency)
    print("— v2 columns")
    ensure_attribute(api, sig, string_attr(
        f"{p}noisereason", "Noise Reason", 100,
        desc="Why the noise gate excluded this signal (v2 WS1); empty = not noise"))
    for a in [
        memo_attr(f"{p}aisummary", "AI Summary", 4000,
                  desc="Cached correspondence summary (v2 WS5)"),
        datetime_attr(f"{p}aisummaryat", "AI Summary Generated"),
        string_attr(f"{p}aisummarywatermark", "AI Summary Watermark", 100,
                    desc="Newest signal timestamp included in the cached summary"),
    ]:
        ensure_attribute(api, "contact", a)
    for a in [
        picklist_attr(f"{p}statedurgency", "Stated Urgency",
                      ["None", "UrgentLanguage", "ExplicitDeadline"], value_base,
                      desc="Set once by the classifier at creation; never re-graded"),
        datetime_attr(f"{p}explicitdeadline", "Explicit Deadline"),
    ]:
        ensure_attribute(api, req, a)
    ensure_option(api, sig, f"{p}matchmethod", "Regarding", value_base + 5)

    # 10. close-readiness directive columns (2026-08-02). Until the next
    # manual PROD solution import lands these, the analysis layer computes
    # Is Primary at export time and routing lives in state/request_routing.json.
    print("— close-readiness columns")
    ensure_attribute(api, sig, bool_attr(
        f"{p}isprimary", "Is Primary", default=True,
        desc="§0.1 canonical attribution of this message within its opportunity "
             "scope; duplicates keep attribution history but are excluded from "
             "volume/latency rollups"))
    ensure_attribute(api, req, picklist_attr(
        f"{p}routingcategory", "Routing Category",
        ["ProcessBlocker", "Conviction", "DealMechanics", "Scheduling"],
        value_base,
        desc="§2.2 four-way routing taxonomy — drives who handles the request"))

    # 11. ir@ triage taxonomy (2026-08-04, docs/ir-triage-categories.md;
    # operator approved). Category options are APPEND-ONLY; the secondary
    # picklist declares all 13 labels in the same order so Choices.req_category
    # values apply to both columns.
    print("— ir@ triage taxonomy columns")
    REQ_CATEGORIES_V2 = ["Reporting", "CapitalAccount", "Valuation", "KYC-AML",
                         "SubscriptionDocs", "Legal-SideLetter", "Meeting",
                         "DataRoom", "Other",
                         "CapitalCall", "TaxDocs", "AccountAdmin",
                         "LiquidityTransfer"]
    for i, name in enumerate(REQ_CATEGORIES_V2[9:], start=9):
        ensure_option(api, req, f"{p}category", name, value_base + i)
    for a in [
        picklist_attr(f"{p}secondarycategory", "Secondary Category",
                      REQ_CATEGORIES_V2, value_base,
                      desc="Second topic when one email spans two categories "
                           "(classifier v2); same option order as Category"),
        bool_attr(f"{p}thirdparty", "Third Party",
                  desc="Sender acts on behalf of an investor (CPA / advisor / "
                       "family office / custodian / auditor) — verification "
                       "rules differ"),
        int_attr(f"{p}classifierconfidence", "Classifier Confidence",
                 minv=0, maxv=100,
                 desc="Classifier's own 0-100; low values feed the review "
                      "queue. AI-generated — see aigenerated/humanconfirmed"),
    ]:
        ensure_attribute(api, req, a)

    # 11b. v3 category taxonomy (2026-08-06, operator-approved): add NDA and put
    # the same category picklist on the engagement signal so every ir@-route
    # email is tagged, not just the ones that become requests. APPEND-ONLY —
    # NDA is index 13 on both request columns; the signal picklist declares all
    # 14 labels in the same order so Choices.req_category values apply verbatim.
    print("— v3 category taxonomy (NDA + signal category)")
    REQ_CATEGORIES_V3 = REQ_CATEGORIES_V2 + ["NDA"]
    ensure_option(api, req, f"{p}category", "NDA", value_base + 13)
    ensure_option(api, req, f"{p}secondarycategory", "NDA", value_base + 13)
    ensure_attribute(api, sig, picklist_attr(
        f"{p}category", "Category", REQ_CATEGORIES_V3, value_base,
        desc="LLM topic category for this email (v3). Same option order/values "
             "as new_inforequest.new_category; written for signals on the "
             "restricted ir@ route (TAG_SIGNAL_CATEGORY). Dormant where absent."))

    # 11c. v4 category taxonomy (2026-08-09, operator-approved): more servicing
    # topics we expect to see. APPEND-ONLY — indices 14/15 on all three category
    # columns (request category, secondary category, signal category).
    print("— v4 category taxonomy (Carta Onboarding + Brokerage Details)")
    for i, name in enumerate(["CartaOnboarding", "BrokerageDetails"], start=14):
        ensure_option(api, req, f"{p}category", name, value_base + i)
        ensure_option(api, req, f"{p}secondarycategory", name, value_base + i)
        ensure_option(api, sig, f"{p}category", name, value_base + i)

    # 12. ir@ intake (docs/ir-intake-design.md, operator-approved 2026-08-05):
    # marker distinguishing auto-created sender contacts from curated CRM rows.
    # The intake path stays dormant in any env where this column is absent.
    print("— ir@ intake column")
    ensure_attribute(api, "contact", string_attr(
        f"{p}autocreatedby", "Auto-Created By", 100,
        desc="ir-intake|<runid> when this contact was auto-created from an "
             "unknown sender to an intake mailbox; empty for curated contacts"))

    n = len(api.creates)
    if args.apply:
        print(f"\nDone — {n} components created (or all existed). "
              "Alternate-key index activation is async; check key status in the maker "
              "portal before first ingestion run.")
        print("NEXT: create the 6 rollup columns per docs/schema.md, then export "
              "declarative source via solution/export.sh.")
    else:
        print(f"\nDRY RUN complete — {n} components would be created. "
              "Re-run with --apply after operator review.")


if __name__ == "__main__":
    main()
