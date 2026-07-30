"""Shared fakes + fixtures. No live calls anywhere (spec: fixture JSON only)."""
import os

os.environ["CLASSIFIER_BACKEND"] = "stub"   # never hit the LLM from unit tests

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ingestion.config import Choices, Config, Rules

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg():
    return Config(
        tenant_id="t", client_id="c", client_secret="s",
        dataverse_url="https://dev.example.crm4.dynamics.com",
        prefix="new_",
        org_domains=["exigentcap.com"],
        mailboxes=["ir@exigentcap.com"],
        choices=Choices(100000000),
        ingest_floor=datetime(2026, 1, 1, tzinfo=timezone.utc),
        fund_lookup="mint_fundorspv",
        rfi_due_bdays=2,
        rfi_reply_status="WaitingExternal",
        rules=Rules.load(Path(__file__).parent.parent / "rules.json"),
    )


@pytest.fixture
def messages():
    return json.loads((FIXTURES / "messages.json").read_text())


# one live opp (Fund II / OPP-0042), one contact (Anna) as its parent contact
OPP_ROWS = [{
    "opportunityid": "11111111-0000-0000-0000-000000000001",
    "name": "Exigent Fund II Investment",
    "new_oppcode": "OPP-0042",
    "new_prospectcode": "EXG-1040",
    "new_live": True,
    "new_aliases": "Fund II\nSynth",
    "new_activemonitoring": True,
    "new_monitoringstartdate": "2026-06-01T00:00:00Z",
    "_parentcontactid_value": "22222222-0000-0000-0000-000000000002",
}]
CONTACT_ROWS = [{
    "contactid": "22222222-0000-0000-0000-000000000002",
    "fullname": "Anna LP",
    "emailaddress1": "Anna.LP@LPFund.com",   # mixed case on purpose
}]
OPP1 = OPP_ROWS[0]["opportunityid"]
ANNA = CONTACT_ROWS[0]["contactid"]


class FakeGraph:
    def __init__(self, messages):
        self.messages = messages
        self.enrich_calls = 0

    def delta_messages(self, mailbox, folder, delta_link=None):
        if folder != "inbox":          # keep fixture simple: all in inbox
            return [], f"delta-{mailbox}-{folder}", False
        return [dict(m) for m in self.messages], f"delta-{mailbox}-{folder}", False

    def enrich(self, mailbox, msg_id):
        self.enrich_calls += 1
        return {"headers": [], "body": f"full body of {msg_id} " + "x" * 50}


class FakeDataverse:
    """In-memory Dataverse honoring the client interface used by sync."""

    def __init__(self, apply=True):
        self.apply = apply
        self.p = "new_"
        self.signals: dict[str, dict] = {}     # id -> server-shaped row
        self.requests: dict[str, dict] = {}
        self.intents: list[str] = []
        self.created = 0
        self.patched = 0
        self._n = 0

    # scope
    def fetch_opportunities(self, fund_lookup="mint_fundorspv"):
        return [dict(r) for r in OPP_ROWS]

    def fetch_connections(self):
        return []

    def fetch_connection_roles(self):
        return []

    def fetch_regarding_map(self, since_iso):
        return {}

    def fetch_contacts(self, ids):
        return [dict(r) for r in CONTACT_ROWS if r["contactid"] in ids]

    # signal reads
    def _serverize(self, payload):
        row = {}
        for k, v in payload.items():
            if k.endswith("@odata.bind"):
                row[f"_{k.split('@')[0]}_value"] = v[v.index("(") + 1:-1]
            else:
                row[k] = v
        return row

    def get_signal(self, keyhash, contact_id):
        for r in self.signals.values():
            if r.get("new_messagekeyhash") == keyhash and \
                    r.get("_new_contact_value") == contact_id:
                return dict(r)
        return None

    def confirmed_conv_opps(self, conv_ids, confirmed_value):
        out = {}
        for r in self.signals.values():
            if r.get("new_matchstatus") == confirmed_value and \
                    r.get("_new_opportunity_value") and \
                    r.get("new_conversationid") in conv_ids:
                out.setdefault(r["new_conversationid"], r["_new_opportunity_value"])
        return out

    def conversation_signals(self, conv_id):
        return [dict(r) for r in self.signals.values()
                if r.get("new_conversationid") == conv_id]

    def requests_for_signals(self, signal_ids, open_values):
        return [dict(r) for r in self.requests.values()
                if r.get("_new_sourcesignal_value") in signal_ids
                and r.get("new_status") in open_values]

    # writes
    def create(self, entity_set, payload, describe):
        self.intents.append(f"CREATE {describe}")
        if not self.apply:
            return None
        self._n += 1
        row = self._serverize(payload)
        if "engagementsignal" in entity_set:
            row["new_engagementsignalid"] = f"sig-{self._n:04d}"
            row.setdefault("new_responselatencymin", None)
            self.signals[row["new_engagementsignalid"]] = row
        else:
            row["new_inforequestid"] = f"req-{self._n:04d}"
            self.requests[row["new_inforequestid"]] = row
        self.created += 1
        return dict(row)

    def patch(self, entity_set, row_id, payload, describe):
        self.intents.append(f"PATCH {describe}")
        if not self.apply:
            return
        store = self.signals if "engagementsignal" in entity_set else self.requests
        store[row_id].update(self._serverize(payload))
        self.patched += 1
