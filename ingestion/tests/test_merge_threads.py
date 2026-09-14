"""Retrospective thread merge: the planner is pure, so the judgement policy is
pinned here without Dataverse or the LLM. Same policy as the ingest-time gate:
only a confident "same ask" folds a follow-up into the thread's first ticket.
"""
from ingestion.merge_threads import plan_thread_merges

MIN = 70


def t(id_, title, received, snippet="", category="Meeting"):
    return {"id": id_, "title": title, "received": received,
            "snippet": snippet, "category": category}


def judge_by_title(same_titles, conf=90):
    """Stub judge: 'same' when the later ticket's title is in same_titles."""
    def judge(subject, body, open_title, open_category):
        return {"same_request": subject in same_titles, "confidence": conf,
                "reason": "stub"}
    return judge


THREAD = [
    t("b", "Asks to move the call to 5:30pm", "2026-09-07T09:03Z"),
    t("a", "Wants to schedule a call", "2026-09-06T14:23Z"),      # earliest
    t("c", "Also asks for the 2025 K-1", "2026-09-07T17:21Z"),
]


def test_earliest_ticket_is_kept_and_followups_are_judged_against_it():
    seen = []
    def judge(subject, body, open_title, open_category):
        seen.append((subject, open_title))
        return {"same_request": True, "confidence": 95, "reason": "same call"}
    plan = plan_thread_merges([THREAD], judge, MIN)
    assert {s for s, _ in seen} == {"Asks to move the call to 5:30pm",
                                     "Also asks for the 2025 K-1"}
    assert all(o == "Wants to schedule a call" for _, o in seen)
    assert {c for c, k, _, _ in plan} == {"b", "c"}
    assert all(k == "a" for _, k, _, _ in plan)


def test_a_different_ask_stays_separate():
    plan = plan_thread_merges([THREAD],
                              judge_by_title({"Asks to move the call to 5:30pm"}), MIN)
    assert [(c, k) for c, k, _, _ in plan] == [("b", "a")]     # 'c' kept


def test_low_confidence_yes_is_not_merged():
    plan = plan_thread_merges([THREAD], judge_by_title({"Asks to move the call to 5:30pm"},
                                                       conf=40), MIN)
    assert plan == []


def test_no_classifier_means_nothing_merges():
    plan = plan_thread_merges([THREAD], lambda *a: None, MIN)
    assert plan == []


def test_single_ticket_threads_are_ignored():
    calls = []
    plan = plan_thread_merges([[THREAD[0]]], lambda *a: calls.append(a) or None, MIN)
    assert plan == [] and calls == []


def test_confidence_is_clamped():
    plan = plan_thread_merges([THREAD[:2]], judge_by_title({THREAD[0]["title"]},
                                                           conf=250), MIN)
    assert plan[0][2] == 100
