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

"""Unit tests for W1 deterministic Playbook generation and injection."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from artemis.agents.flash.workflow_hook import FlashWorkflowHook
from artemis.workflow.playbook import build_playbook, main_path, next_playbook_step
from artemis.workflow.recorder import RawScreen, RecordedStep, record_run
from artemis.workflow.store import Edge, Node, Workflow, WorkflowStore
from artemis.workflow.targets import TargetDescriptor


PKG = "com.example.clock"


def _n(rid: str, l: int, t: int, r: int, b: int, *, text: str = "", cls: str = "android.widget.Button", clickable: bool = True, editable: bool = False, in_scrollable: bool = False) -> dict:
    return {
        "resource-id": f"{PKG}:id/{rid}" if rid else "",
        "class": cls,
        "bounds": f"[{l},{t}][{r},{b}]",
        "clickable": str(clickable).lower(),
        "editable": str(editable).lower(),
        "enabled": "true",
        "visible-to-user": "true",
        "package": PKG,
        "text": text,
        "_in_scrollable": in_scrollable,
    }


def _clock_home() -> list[dict]:
    return [
        _n("title", 20, 20, 300, 80, text="Clock", cls="android.widget.TextView", clickable=False),
        _n("fab", 400, 700, 600, 850, text="Add city"),
        _n("tab1", 20, 900, 300, 980, text="Alarm"),
        _n("tab2", 350, 900, 650, 980, text="Clock", clickable=False),
        _n("cities", 20, 150, 980, 650, text="Tokyo", cls="android.widget.TextView", clickable=False),
    ]


def _search_page(typed: str = "") -> list[dict]:
    return [
        _n("search_box", 100, 40, 900, 120, text=typed, cls="android.widget.EditText", editable=True),
        _n("clear", 900, 40, 980, 120, text="Clear"),
        _n("empty", 200, 300, 800, 400, text="Search for a city", cls="android.widget.TextView", clickable=False),
        _n("kbd_search", 850, 850, 990, 930, text="Search"),
        _n("back", 10, 40, 90, 120, text="Back"),
    ]


def _results_page(city: str) -> list[dict]:
    return [
        _n("search_box", 100, 40, 900, 120, text=city, cls="android.widget.EditText", editable=True),
        _n("rv", 0, 150, 1000, 900, cls="android.support.v7.widget.RecyclerView", clickable=False),
        _n("city_row", 50, 180, 950, 260, text=f"{city}, Ireland", cls="android.widget.TextView", in_scrollable=True),
        _n("city_row", 50, 280, 950, 360, text=f"{city}, CA, USA", cls="android.widget.TextView", in_scrollable=True),
        _n("back", 10, 40, 90, 120, text="Back"),
    ]


def test_recorder_saves_label_and_description_for_judgement_and_slots():
    s1 = RawScreen(_clock_home(), 1000, 1000, seq=1)
    s2 = RawScreen(_search_page(), 1000, 1000, seq=2)
    s3 = RawScreen(_search_page("Madrid"), 1000, 1000, seq=3)
    s4 = RawScreen(_results_page("Madrid"), 1000, 1000, seq=4)
    s5 = RawScreen(_clock_home(), 1000, 1000, seq=5)

    steps = [
        RecordedStep(
            tool="click",
            args={"target": 1, "target_description": "Add city button"},
            action={"normalized_coordinates": [500, 775], "target_bounds": [400, 700, 600, 850], "target_resource_id": f"{PKG}:id/fab"},
            pre=s1,
            post=s2,
        ),
        RecordedStep(
            tool="input_text",
            args={"text": "Madrid", "target_description": "City search box"},
            action={"normalized_coordinates": [500, 80], "target_bounds": [100, 40, 900, 120]},
            pre=s2,
            post=s3,
        ),
        RecordedStep(
            tool="click",
            args={"target": 4, "target_description": "Search key"},
            action={"normalized_coordinates": [920, 890], "target_bounds": [850, 850, 990, 930], "target_resource_id": f"{PKG}:id/kbd_search"},
            pre=s3,
            post=s4,
        ),
        RecordedStep(
            tool="click",
            args={"target": 3, "target_description": "Pick Madrid, Ireland option in results"},
            action={"normalized_coordinates": [500, 220], "target_bounds": [50, 180, 950, 260], "target_resource_id": f"{PKG}:id/city_row"},
            pre=s4,
            post=s5,
            after_user_choice=True,
        ),
    ]
    wf = record_run(steps, goal="Add the city Madrid to world clock")
    assert wf is not None
    assert wf.start_nodes == ["n1"]
    judgements = [e for e in wf.edges if e.kind == "judgement"]
    assert len(judgements) == 1
    assert "{slot1}" in judgements[0].label
    assert "{slot1}" in judgements[0].description

    pb = build_playbook(wf, {"slot1": "Dublin"})
    assert "Dublin" in pb
    assert "Madrid" not in pb
    assert "Add city" in pb
    assert "choose on screen" in pb


def test_main_path_skips_disabled_and_picks_higher_ok_branch():
    wf = Workflow(
        workflow_id="w__1",
        package=PKG,
        goal="Do task",
        start_nodes=["n1"],
        end_nodes=["n4"],
        nodes={"n1": Node("n1"), "n2": Node("n2"), "n3": Node("n3"), "n4": Node("n4")},
        edges=[
            Edge("e_bad", "n1", "n2", "nav", tool="click", state="disabled", label="Wrong"),
            Edge("e1", "n1", "n2", "nav", tool="click", ok=2, label="Start"),
            Edge("e2_low", "n2", "n3", "nav", tool="click", ok=0, label="Old path"),
            Edge("e2_hi", "n2", "n4", "nav", tool="click", ok=5, label="Fast path"),
            Edge("e3", "n3", "n4", "nav", tool="click", ok=0, label="Finish old"),
        ],
    )
    path = main_path(wf)
    assert [e.id for e in path] == ["e1", "e2_hi"]

    pb = build_playbook(wf, {}, done_edge_ids={"e1"}, show_progress=True)
    assert "1) [done] tap 'Start'" in pb
    assert "2) [NEXT] tap 'Fast path'" in pb
    assert "Wrong" not in pb and "Old path" not in pb
    assert next_playbook_step(wf, {}, {"e1"}) == "tap 'Fast path'"


def test_hook_injects_playbook_on_turn1_even_when_autopilot_runs_zero_steps(tmp_path):
    wf = Workflow(
        workflow_id=f"{PKG}__1",
        package=PKG,
        goal="Search Dublin on web",
        slots={"slot1": "Madrid"},
        start_nodes=["n1"],
        end_nodes=["n2"],
        nodes={"n1": Node("n1"), "n2": Node("n2")},
        edges=[
            Edge("e1", "n1", "n2", "slot", tool="input_text", slot="slot1", target=TargetDescriptor(focused_editable=True), description="address bar"),
        ],
    )
    WorkflowStore(tmp_path).save(wf)

    # Live screen belongs to PKG but does not match n1 -> Autopilot runs 0 steps, yet Turn 1 gets Playbook.
    unmatched_live = [
        _n("other1", 10, 10, 200, 100, text="Other"),
        _n("other2", 10, 150, 200, 250, text="Page"),
        _n("other3", 10, 300, 200, 400, text="Link"),
        _n("other4", 10, 450, 200, 550, text="Btn"),
        _n("other5", 10, 600, 200, 700, text="Text"),
    ]
    runner = SimpleNamespace(
        ctx=SimpleNamespace(last_raw_observation={"elements": unmatched_live, "width": 1000, "height": 1000, "seq": 1}),
        goal="Search Dublin on web",
        _llm=None,
        _force_image=False,
    )
    hook = FlashWorkflowHook(runner, "replay", str(tmp_path))
    # Pre-select without LLM (exact/mocked selector)
    hook.workflow = wf
    hook.slot_values = {"slot1": "Dublin"}
    from artemis.workflow.autopilot import Autopilot

    hook._autopilot = Autopilot(wf, {"slot1": "Dublin"})

    res = asyncio.run(hook.maybe_autopilot(SimpleNamespace(screen_scrollable=False), b"img", []))
    assert res is not None
    _, _, notice = res
    assert "[Playbook]" in notice
    assert "Dublin" in notice
    # Turn 2 with still 0 steps should NOT re-send the full playbook.
    res2 = asyncio.run(hook.maybe_autopilot(SimpleNamespace(screen_scrollable=False), b"img", []))
    assert res2 is None
