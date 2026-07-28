"""Acceptance criteria: re-running ingestion on the same input produces zero
new/changed rows; provenance always populated; N-contact messages create one
row per contact but dedupe by messagekeyhash at the message level."""
import json

from ingestion.sync import SyncRun
from ingestion.tests.conftest import FakeDataverse, FakeGraph, ANNA, OPP1


def run_once(cfg, tmp_path, dv=None, graph=None, messages=None):
    dv = dv or FakeDataverse(apply=True)
    graph = graph or FakeGraph(messages)
    run = SyncRun(cfg, graph, dv, apply=True, state_dir=tmp_path, codeversion="test")
    log = run.run()
    return dv, graph, log


def test_first_run_creates_expected_rows(cfg, messages, tmp_path):
    dv, graph, log = run_once(cfg, tmp_path, messages=messages)
    # msg-001 (inbound, Anna) + msg-002 (outbound reply, Anna) + msg-004
    # (autoreply, still stored with ismeaningful=false) = 3 signals.
    # msg-003 internal-only → no matched contact; msg-005 excluded keyword.
    assert dv.created == 3
    assert log["counts"]["no_contact"] == 1
    assert log["counts"]["excluded"] == 1
    for row in dv.signals.values():
        assert row["new_provenance"], "provenance must always be populated"
        assert row["_new_contact_value"] == ANNA
        assert row["_new_opportunity_value"] == OPP1   # single-opp ContactMatch
    autoreply = [r for r in dv.signals.values()
                 if r["new_name"].startswith("Automatic reply")]
    assert autoreply and autoreply[0]["new_ismeaningful"] is False


def test_latency_written_on_thread(cfg, messages, tmp_path):
    dv, _, _ = run_once(cfg, tmp_path, messages=messages)
    out = [r for r in dv.signals.values()
           if r["new_direction"] == cfg.choices.direction["Outbound"]]
    assert out and out[0]["new_responselatencymin"] == 90   # 09:00 → 10:30


def test_second_run_is_zero_writes(cfg, messages, tmp_path):
    dv, _, _ = run_once(cfg, tmp_path, messages=messages)
    created_before, patched_before = dv.created, dv.patched
    run_once(cfg, tmp_path, dv=dv, messages=messages)   # same store, same input
    assert dv.created == created_before, "re-run must create nothing"
    assert dv.patched == patched_before, "re-run must change nothing"


def test_dry_run_writes_nothing_and_keeps_tokens(cfg, messages, tmp_path):
    dv = FakeDataverse(apply=False)
    graph = FakeGraph(messages)
    run = SyncRun(cfg, graph, dv, apply=False, state_dir=tmp_path, codeversion="test")
    log = run.run()
    assert dv.created == 0 and dv.patched == 0
    assert not (tmp_path / "delta").exists(), "dry-run must not advance delta tokens"
    assert log["dv_intents"] > 0            # intent log present for operator review
    runlog = json.loads((tmp_path / "runs" / f"{log['runid']}.json").read_text())
    assert runlog["apply"] is False
