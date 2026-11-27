# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Picks the recorded workflow for a new goal and fills its slots (KE_HOACH Mục 6.1).

One text-only LLM call. Its answer is never trusted blindly: a slot value
must literally appear in the new goal, otherwise the slot stays unknown and
the steps that need it go to the AI.
"""

from __future__ import annotations

import json
import re
from typing import Awaitable, Callable

from artemis.workflow.store import Workflow

AskText = Callable[[str], Awaitable[str]]


def build_prompt(goal: str, candidates: list[Workflow]) -> str:
    lines = [
        "You map a new phone task onto a workflow recorded earlier in the same app.",
        "Recorded workflows:",
    ]
    for i, wf in enumerate(candidates, 1):
        lines.append(f"{i}) task: {json.dumps(wf.goal, ensure_ascii=False)}; slots: {json.dumps(wf.slots, ensure_ascii=False)}")
    lines += [
        f"New task: {json.dumps(goal, ensure_ascii=False)}",
        "Pick a workflow only if the new task is the same kind of task (same steps),"
        " differing at most in the slot values. For each slot give the value for the"
        " new task copied exactly from the new task text, or null if it has none.",
        'Answer with JSON only: {"workflow": <number or null>, "slots": {"slot1": "..."}}',
    ]
    return "\n".join(lines)


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def select_workflow(
    goal: str, candidates: list[Workflow], ask: AskText | None
) -> tuple[Workflow | None, dict[str, str]]:
    if not candidates:
        return None, {}
    # Same goal verbatim: no LLM needed, the recorded values apply as-is.
    for wf in candidates:
        if wf.goal.strip().casefold() == goal.strip().casefold():
            return wf, dict(wf.slots)
    if ask is None:
        return None, {}
    try:
        data = _parse(await ask(build_prompt(goal, candidates)))
    except Exception:
        return None, {}
    idx = data.get("workflow")
    if not isinstance(idx, int) or not 1 <= idx <= len(candidates):
        return None, {}
    wf = candidates[idx - 1]
    g = goal.casefold()
    values: dict[str, str] = {}
    raw = data.get("slots") if isinstance(data.get("slots"), dict) else {}
    for name in wf.slots:
        v = raw.get(name)
        if isinstance(v, str) and v.strip() and v.strip().casefold() in g:
            values[name] = v.strip()
    return wf, values
