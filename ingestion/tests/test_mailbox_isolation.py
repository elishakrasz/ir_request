"""One unreadable mailbox must not kill the run for the others (2026-09-14).

Found adding kbardash@ to the IR route: the restricted app is blocked for it by
the Application Access Policy until that is extended, so every tick would have
raised 403 in the delta walk and taken ir@/mravid@/lgruber@ down with it.

The failure is isolated per mailbox and is NOT counted in c["errors"] — that
counter withholds delta tokens for every mailbox, and the healthy ones must
keep advancing; the failed pair just retries from its last token next tick.
"""
import json

from ingestion.sync import SyncRun
from ingestion.tests.conftest import FakeDataverse, FakeGraph

GOOD, BAD = "ir@exigentcap.com", "kbardash@exigentcap.com"


class OneBadMailbox(FakeGraph):
    def delta_messages(self, mailbox, folder, delta_link=None):
        if mailbox == BAD:
            raise RuntimeError("403 ErrorAccessDenied: [RAOP] Blocked by tenant "
                               "configured App")
        return super().delta_messages(mailbox, folder, delta_link)


def test_bad_mailbox_is_isolated_and_good_one_still_advances(cfg, messages, tmp_path):
    dv = FakeDataverse(apply=True)
    run = SyncRun(cfg, OneBadMailbox(messages), dv, apply=True,
                  state_dir=tmp_path, codeversion="test")
    log = run.run(mailboxes=[GOOD, BAD])            # must not raise
    c = log["counts"]

    # the good mailbox was processed as normal
    assert c["mailboxes"][f"{GOOD}/inbox"]["fetched"] == len(messages)
    assert dv.created > 0

    # the bad one is recorded, per folder, with the reason
    assert c["mailbox_errors"] == 2                 # inbox + sentitems
    for folder in ("inbox", "sentitems"):
        entry = c["mailboxes"][f"{BAD}/{folder}"]
        assert entry["fetched"] == 0
        assert "Blocked by tenant" in entry["error"]
    assert any(BAD in e for e in log["errors"])

    # crucially: NOT a run-level error, so delta tokens still advance...
    assert c.get("errors", 0) == 0
    good_token = tmp_path / "delta" / f"{GOOD}__inbox.json"
    assert good_token.exists()
    assert json.loads(good_token.read_text())["deltaLink"] == f"delta-{GOOD}-inbox"
    # ...for the good mailbox only; the bad one has nothing to save
    assert not (tmp_path / "delta" / f"{BAD}__inbox.json").exists()


def test_all_good_is_unchanged(cfg, messages, tmp_path):
    dv = FakeDataverse(apply=True)
    run = SyncRun(cfg, FakeGraph(messages), dv, apply=True,
                  state_dir=tmp_path, codeversion="test")
    log = run.run(mailboxes=[GOOD])
    assert "mailbox_errors" not in log["counts"]
    assert all("error" not in v for v in log["counts"]["mailboxes"].values())
