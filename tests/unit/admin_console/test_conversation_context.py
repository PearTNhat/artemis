"""Tests for chat follow-ups carrying the previous task's context."""

import json

try:
    from admin_console.services.conversation_context import (
        CONTEXT_MARKER,
        MAX_EARLIER_REQUESTS,
        build_followup_goal,
        split_goal,
    )
except ImportError:
    from apps.admin_console.services.conversation_context import (
        CONTEXT_MARKER,
        MAX_EARLIER_REQUESTS,
        build_followup_goal,
        split_goal,
    )


def _step(n, action, thought=""):
    return {"step_number": n, "action_taken": json.dumps(action), "operator_raw_thinking": thought}


GRAB_STEPS = [
    _step(1, {"action": "click", "target_text": "Food"}, "Opening GrabFood."),
    _step(
        2,
        {"action": "click", "target_description": "green Basket 1 Item button"},
        "Added Phá lấu; opening the basket. Will stop before ordering.",
    ),
]


def test_no_previous_session_keeps_the_request():
    assert build_followup_goal("open YouTube", None, []) == "open YouTube"


def test_followup_carries_request_outcome_and_steps():
    previous = {"initial_goal": "đặt 1 món trên Grab, đừng bấm đặt", "status": "cancelled"}
    goal = build_followup_goal("tiếp tục đi", previous, GRAB_STEPS)

    request, _, context = goal.partition("\n\n")
    assert request == "tiếp tục đi"
    assert context.startswith(CONTEXT_MARKER)
    assert "Previous request: đặt 1 món trên Grab, đừng bấm đặt" in goal
    assert "Outcome: cancelled" in goal
    assert "- click(Food): Opening GrabFood." in goal
    assert "click(green Basket 1 Item button)" in goal


def test_final_report_becomes_the_outcome_not_a_step():
    steps = [
        _step(1, {"action": "click", "target_text": "Song"}, "Playing."),
        _step(
            2,
            {"action": "report_task_status", "args": {"status": "completed", "explanation": "Song is playing."}},
        ),
    ]
    goal = build_followup_goal("pause it", {"initial_goal": "play song", "status": "completed"}, steps)
    assert "Outcome: completed — Song is playing." in goal
    assert "report_task_status" not in goal


def test_chain_carries_earlier_requests_without_nesting():
    goal = "first"
    for i in range(1, 9):
        goal = build_followup_goal(f"request {i}", {"initial_goal": goal, "status": "completed"}, [])
    request, earlier = split_goal(goal)
    assert request == "request 8"
    assert goal.count(CONTEXT_MARKER) == 1
    # The immediate previous request plus at most MAX_EARLIER_REQUESTS older ones.
    assert earlier[-1] == "request 7"
    assert len(earlier) == MAX_EARLIER_REQUESTS + 1


def test_split_goal_without_context():
    assert split_goal("just a task") == ("just a task", [])
