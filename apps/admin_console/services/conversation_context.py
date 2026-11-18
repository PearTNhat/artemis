"""Chat follow-ups: carry what the previous task did into the next request.

Every task runs with a fresh agent context. When the user keeps typing in the
same chat after a task ended ("now open the basket"), the new goal is prefixed
with a compact summary of the previous task (its request, outcome and last
steps) so the agent understands what "it", "the app" or "continue" refers to.
Older requests in the chain are carried forward as one-line entries, so the
context stays bounded however long the chat gets.
"""

from __future__ import annotations

import json
from typing import Any

CONTEXT_MARKER = "[Chat context: earlier tasks in this conversation]"
_EARLIER_HEADER = "Earlier requests:"
_PREVIOUS_HEADER = "Previous request:"

MAX_EARLIER_REQUESTS = 5
MAX_STEPS = 8
MAX_LINE_CHARS = 220
MAX_REQUEST_CHARS = 300


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def split_goal(goal: str) -> tuple[str, list[str]]:
    """Splits a stored goal into (user request, earlier requests it carried).

    A goal built by :func:`build_followup_goal` carries its own context block;
    its earlier and previous requests become the chain's earlier requests.
    """
    request, sep, context = (goal or "").partition(f"\n\n{CONTEXT_MARKER}")
    if not sep:
        return request.strip(), []
    earlier: list[str] = []
    in_earlier = False
    for line in context.splitlines():
        stripped = line.strip()
        if stripped == _EARLIER_HEADER:
            in_earlier = True
        elif in_earlier and stripped.startswith("- "):
            earlier.append(stripped[2:])
        else:
            in_earlier = False
            if stripped.startswith(_PREVIOUS_HEADER):
                earlier.append(stripped[len(_PREVIOUS_HEADER) :].strip())
    return request.strip(), earlier


def _step_line(step: dict[str, Any]) -> tuple[str | None, str | None]:
    """Returns (step summary line, final explanation if it is the report step)."""
    try:
        action = json.loads(step.get("action_taken") or "{}")
    except (TypeError, ValueError):
        action = {}
    name = action.get("action") or "step"
    args = action.get("args") or {}
    if name == "report_task_status":
        return None, args.get("explanation")
    target = (
        action.get("target_text")
        or action.get("target_description")
        or args.get("text")
        or args.get("app_name")
        or args.get("direction")
        or ""
    )
    thought = _clip(step.get("operator_raw_thinking") or "", MAX_LINE_CHARS)
    head = f"{name}({_clip(str(target), 60)})" if target else name
    return (f"{head}: {thought}" if thought else head), None


def build_followup_goal(
    new_request: str,
    previous_session: dict[str, Any] | None,
    previous_steps: list[dict[str, Any]],
) -> str:
    """The goal to run: the new request plus the previous task's context."""
    new_request = (new_request or "").strip()
    if not previous_session:
        return new_request

    prev_request, earlier = split_goal(str(previous_session.get("initial_goal") or ""))
    earlier = [_clip(r, MAX_REQUEST_CHARS) for r in earlier][-MAX_EARLIER_REQUESTS:]

    lines: list[str] = []
    explanation = None
    for step in previous_steps[-MAX_STEPS:]:
        line, final = _step_line(step)
        if final:
            explanation = final
        if line:
            lines.append(line)

    status = str(previous_session.get("status") or "unknown")
    outcome = status + (f" — {_clip(explanation, 400)}" if explanation else "")

    parts = [
        new_request,
        "",
        CONTEXT_MARKER,
        "The request above continues this chat. Use the context below only to"
        " understand what the user refers to; the device screen may have changed"
        " since, so judge the current state from the live screen.",
    ]
    if earlier:
        parts.append(_EARLIER_HEADER)
        parts.extend(f"- {r}" for r in earlier)
    parts.append(f"{_PREVIOUS_HEADER} {_clip(prev_request, MAX_REQUEST_CHARS)}")
    parts.append(f"Outcome: {outcome}")
    if lines:
        parts.append("Last steps of that task:")
        parts.extend(f"- {line}" for line in lines)
    return "\n".join(parts)
