"""The final review checks the user's whole goal, not only the lines the plan declared.

Regression (Pro Shopee trace 64c570f2): the plan declared one line ("stop at
checkout without ordering"); the review judged that line alone and reported 1/1
passed, never looking at the Mall tag, the cheapest choice, the variant or the
quantity the user asked for.
"""

from artemis.agents.checker.checker import (
    GOAL_ITEM_PREFIX,
    CheckReport,
    GoalRequirement,
    merge_goal_requirements,
)
from artemis.graph.checkpoints import compute_test_summary
from artemis.graph.graph import _goal_items_from_ledger


def _report(**kw):
    return CheckReport.model_validate(kw)


def test_goal_requirements_become_goal_check_verdicts():
    report = _report(
        verdicts=[{"item_text": "stop at checkout", "kind": "assert", "status": "passed", "evidence": "screen"}],
        goal_requirements=[
            {"requirement": "product has the Mall tag", "kind": "verify", "status": "passed", "evidence": "Mall badge"},
            {"requirement": "cheapest Mall product among the first 10", "kind": "verify", "status": "failed",
             "evidence": "an 8.000d Mall cable was listed", "suggestion": "pick the 8.000d cable"},
        ],
    )
    merged = merge_goal_requirements(report)
    goal = [v for v in merged.verdicts if v.item_text.startswith(GOAL_ITEM_PREFIX)]
    assert [(v.item_text, v.kind, v.status) for v in goal] == [
        ("[goal] product has the Mall tag", "verify", "passed"),
        ("[goal] cheapest Mall product among the first 10", "verify", "failed"),
    ]
    assert goal[1].suggestion == "pick the 8.000d cable"


def test_requirement_already_declared_is_not_added_twice():
    report = _report(
        verdicts=[{"item_text": "Stop at checkout", "kind": "assert", "status": "passed", "evidence": "x"},
                  {"item_text": "[goal] quantity is 1", "kind": "verify", "status": "passed", "evidence": "x"}],
        goal_requirements=[
            {"requirement": "stop at  checkout", "kind": "assert", "status": "passed", "evidence": "x"},
            {"requirement": "quantity is 1", "kind": "verify", "status": "passed", "evidence": "x"},
        ],
    )
    assert len(merge_goal_requirements(report).verdicts) == 2


def test_malformed_requirement_is_repaired_and_never_passes_by_default():
    req = GoalRequirement.model_validate({"item_text": "variant is black 1m", "evidence": "?"})
    assert (req.requirement, req.kind, req.status) == ("variant is black 1m", "verify", "inconclusive")
    req = GoalRequirement.model_validate({"requirement": "no order placed", "kind": "assert", "verdict": "passed"})
    assert req.status == "passed"


def test_a_failed_goal_requirement_counts_in_the_test_summary():
    records = [
        {"kind": "assert", "item_text": "stop at checkout", "status": "passed", "evidence": "x"},
        {"kind": "verify", "item_text": "[goal] cheapest Mall product", "status": "failed", "evidence": "8.000d listed"},
    ]
    summary = compute_test_summary([], records)
    assert (summary.passed, summary.failed) == (1, 1)
    assert summary.failed_items[0]["item_text"] == "[goal] cheapest Mall product"


def test_goal_lines_are_redeclared_on_the_next_final_attempt():
    ledger = [
        {"kind": "verify", "item_text": "[goal] cheapest Mall product", "status": "failed"},
        {"kind": "verify", "item_text": "[goal] cheapest Mall product", "status": "failed"},
        {"kind": "assert", "item_text": "stop at checkout", "status": "passed"},
    ]
    items = _goal_items_from_ledger(ledger)
    assert [(i.kind, i.when, i.text) for i in items] == [("verify", "at_end", "[goal] cheapest Mall product")]


def test_final_prompt_asks_for_every_goal_requirement():
    from artemis.agents.checker.checker import _load_prompts

    assert "goal_requirements" in _load_prompts()["final_guide"]


def test_planner_prompt_covers_user_stated_requirements():
    import json
    from pathlib import Path

    text = Path("artemis/agents/planner/planner.json").read_text(encoding="utf-8")
    assert "Goal coverage (user-stated requirements)" in json.loads(json.dumps(text))
