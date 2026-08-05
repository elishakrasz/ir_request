"""ir@ intake path (docs/ir-intake-design.md): unknown senders to an intake
mailbox get a find-or-create lightweight contact and land in the review queue;
scope, noise, and idempotency guardrails hold."""
from datetime import datetime, timezone

from ingestion.sync import SyncRun
from ingestion.tests.conftest import FakeDataverse, FakeGraph

BASE = 100000000


def make_msg(sender="cpa@accountingfirm.com", name="Chelsea Schuler",
             mailbox="ir@exigentcap.com", intake=True, noise_reason=None):
    return {
        "id": "graph-id-1",
        "subject": "2024 K-1 for River Oaks",
        "bodyPreview": "Can you send the 2024 K-1? Our client needs it.",
        "conversationId": "CONV-INTAKE-1",
        "internetMessageId": "<msg1@x>",
        "from": {"emailAddress": {"address": sender, "name": name}},
        "webLink": "https://outlook/x",
        "_ts": datetime(2026, 8, 4, 9, tzinfo=timezone.utc),
        "_sender": sender,
        "_recipients": ["ir@exigentcap.com"],
        "_participants": [sender, "ir@exigentcap.com"],
        "_hash": "a" * 64,
        "_mailbox": mailbox,
        "_folder": "inbox",
        "_contacts": {},          # unknown sender — no scope match
        "_intake": intake,
        "_noise_reason": noise_reason,
    }


def make_run(cfg, tmp_path, dv):
    cfg.intake_mailboxes = ["ir@exigentcap.com"]
    return SyncRun(cfg, FakeGraph([]), dv, apply=True, state_dir=tmp_path,
                   codeversion="test")


def test_intake_creates_marked_contact_and_unmatched_signal(cfg, tmp_path):
    dv = FakeDataverse(apply=True)
    run = make_run(cfg, tmp_path, dv)
    run._handle(make_msg(), {}, {}, {}, {})
    contact = next(iter(dv.contacts.values()))
    assert contact["emailaddress1"] == "cpa@accountingfirm.com"
    assert contact["firstname"] == "Chelsea"
    assert contact["lastname"] == "Schuler"
    assert contact["new_autocreatedby"].startswith("ir-intake|")
    sig = next(iter(dv.signals.values()))
    assert sig["new_matchstatus"] == BASE + 2          # Unmatched → review queue
    assert sig["_new_contact_value"] == contact["contactid"]
    assert run.counts["intake_contacts"] == 1
    assert run.counts["no_contact"] == 0


def test_intake_reuses_existing_crm_contact(cfg, tmp_path):
    dv = FakeDataverse(apply=True)
    dv.contacts["con-pre"] = {"contactid": "con-pre", "fullname": "Chelsea S",
                              "emailaddress2": "CPA@AccountingFirm.com"}
    run = make_run(cfg, tmp_path, dv)
    run._handle(make_msg(), {}, {}, {}, {})
    assert len(dv.contacts) == 1                       # nothing new created
    sig = next(iter(dv.signals.values()))
    assert sig["_new_contact_value"] == "con-pre"
    assert "intake_contacts" not in run.counts


def test_non_intake_mailbox_keeps_old_behavior(cfg, tmp_path):
    dv = FakeDataverse(apply=True)
    run = make_run(cfg, tmp_path, dv)
    run._handle(make_msg(mailbox="edavis@exigentcap.com", intake=False),
                {}, {}, {}, {})
    assert not dv.contacts and not dv.signals
    assert run.counts["no_contact"] == 1


def test_intake_never_creates_contact_for_noise(cfg, tmp_path):
    dv = FakeDataverse(apply=True)
    run = make_run(cfg, tmp_path, dv)
    run._handle(make_msg(sender="exigentcap.ir@apexgroup.com",
                         noise_reason="admin_blast:exigentcap.ir@apexgroup.com"),
                {}, {}, {}, {})
    assert not dv.contacts
    assert run.counts["no_contact"] == 1               # skipped, not written


def test_intake_idempotent_rerun_no_new_rows(cfg, tmp_path):
    dv = FakeDataverse(apply=True)
    run = make_run(cfg, tmp_path, dv)
    run._handle(make_msg(), {}, {}, {}, {})
    run2 = make_run(cfg, tmp_path, dv)                 # fresh run, same message
    run2._handle(make_msg(), {}, {}, {}, {})
    assert len(dv.contacts) == 1
    assert len(dv.signals) == 1
    assert run2.counts["unchanged"] == 1
