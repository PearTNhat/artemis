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

"""Unit tests for W2 zero-LLM goal template selector, index.json, and live-screen ranking."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from artemis.agents.flash.workflow_hook import FlashWorkflowHook
from artemis.workflow.fingerprint import app_fingerprint, normalize
from artemis.workflow.selector import (
    filter_by_live_screen,
    match_goal_template,
    select_workflow,
    select_workflow_fast,
)
from artemis.workflow.store import Edge, INDEX_FILENAME, Node, Workflow, WorkflowStore


PKG = "com.google.android.deskclock"


def _fp(pkg: str, rids: list[str]):
    nodes = [
        {
            "resource-id": f"{pkg}:id/{rid}",
            "class": "android.widget.Button",
            "bounds": f"[10,{10 + i * 80}][900,{70 + i * 80}]",
            "clickable": "true",
            "visible-to-user": "true",
            "package": pkg,
            "text": rid,
        }
        for i, rid in enumerate(rids)
    ]
    return app_fingerprint(normalize(nodes, width=1000, height=1000))


def test_match_goal_template_single_and_repeated_slot():
    wf_goal = "Open the Clock app, go to the Clock tab, add the city Madrid to the world clock list. Stop once Madrid appears in the list."
    new_goal = "Open the Clock app, go to the Clock tab, add the city Dublin to the world clock list. Stop once Dublin appears in the list."
    res = match_goal_template(new_goal, wf_goal, {"slot1": "Madrid"})
    assert res == {"slot1": "Dublin"}


def test_match_goal_template_multi_slot_with_multi_word_value():
    wf_goal = "Open Contacts and create a new contact with first name Minh, last name Tran and phone number 0901234567, then save it."
    new_goal = "Open Contacts and create a new contact with first name Nguyen Van An, last name Pham and phone number 0987654321, then save it."
    res = match_goal_template(
        new_goal,
        wf_goal,
        {"slot1": "Minh", "slot2": "Tran", "slot3": "0901234567"},
    )
    assert res == {"slot1": "Nguyen Van An", "slot2": "Pham", "slot3": "0987654321"}


def test_match_goal_template_rejects_adjacent_slots_and_different_framing():
    # Two slots adjacent without static separator -> reject (must fall back to LLM).
    assert match_goal_template("Add Nguyen An", "Add Minh Tran", {"slot1": "Minh", "slot2": "Tran"}) is None
    # Different static frame -> reject.
    assert (
        match_goal_template(
            "Add city Dublin",
            "Open the Clock app, add the city Madrid to world clock",
            {"slot1": "Madrid"},
        )
        is None
    )
    # Inconsistent repeated slot occurrence -> reject.
    assert (
        match_goal_template(
            "Add Madrid then verify Dublin",
            "Add Madrid then verify Madrid",
            {"slot1": "Madrid"},
        )
        is None
    )


def test_select_workflow_fast_uses_zero_llm_calls(tmp_path):
    wf = Workflow(
        workflow_id=f"{PKG}__1",
        package=PKG,
        goal="Open Clock and add city Madrid. Verify Madrid.",
        slots={"slot1": "Madrid"},
        nodes={"n1": Node("n1", [_fp(PKG, ["a", "b", "c", "d", "e"])])},
        edges=[Edge("e1", "n1", "n1", "slot", tool="input_text", slot="slot1")],
    )
    store = WorkflowStore(tmp_path)
    store.save(wf)

    assert (store._dir(PKG) / INDEX_FILENAME).is_file()
    assert len(store.list(PKG)) == 1  # index.json must not be parsed as a Workflow

    # Corrupt / remove index.json -> list_index rebuilds it cleanly.
    (store._dir(PKG) / INDEX_FILENAME).unlink()
    rebuilt = store.list_index(PKG)
    assert len(rebuilt) == 1 and rebuilt[0].has_runnable is True

    calls = 0

    async def boom_llm(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return "{}"

    runner = SimpleNamespace(ctx=SimpleNamespace(), goal="Open Clock and add city Tokyo. Verify Tokyo.", _llm=None)
    hook = FlashWorkflowHook(runner, "replay", str(tmp_path))
    hook._ask_text = boom_llm
    asyncio.run(hook._select(PKG))
    assert calls == 0
    assert hook.selector_calls == 0
    assert hook.workflow is not None
    assert hook.slot_values == {"slot1": "Tokyo"}


def test_filter_by_live_screen_prioritizes_when_matched_and_falls_back_when_unmatched():
    fp_clock = _fp(PKG, ["cities", "fab", "tab_clock", "tab_alarm", "title"])
    fp_timer = _fp(PKG, ["timer_display", "start", "reset", "tab_timer", "title"])
    fp_other = _fp(PKG, ["x1", "x2", "x3", "x4", "x5"])

    wf_clock = Workflow(f"{PKG}__1", PKG, "Add city Madrid", {"slot1": "Madrid"}, nodes={"n1": Node("n1", [fp_clock])})
    wf_timer = Workflow(f"{PKG}__2", PKG, "Set timer 5 minutes", {"slot1": "5"}, nodes={"n1": Node("n1", [fp_timer])})

    assert [w.workflow_id for w in filter_by_live_screen([wf_clock, wf_timer], fp_clock)] == [f"{PKG}__1"]
    # Unmatched screen (e.g. Launcher or different webpage) -> falls back to all candidates!
    assert [w.workflow_id for w in filter_by_live_screen([wf_clock, wf_timer], fp_other)] == [f"{PKG}__1", f"{PKG}__2"]


def test_select_workflow_falls_back_to_llm_on_rephrased_goal():
    wf1 = Workflow(f"{PKG}__1", PKG, "Add city Madrid", {"slot1": "Madrid"})
    wf2 = Workflow(f"{PKG}__2", PKG, "Set timer 5m", {"slot1": "5m"})

    called = 0

    async def ask(_p: str) -> str:
        nonlocal called
        called += 1
        return '{"workflow": 1, "slots": {"slot1": "Oslo"}}'

    wf, vals = asyncio.run(select_workflow("Please add Oslo to my world clocks", [wf1, wf2], ask))
    assert called == 1
    assert wf is wf1
    assert vals == {"slot1": "Oslo"}

