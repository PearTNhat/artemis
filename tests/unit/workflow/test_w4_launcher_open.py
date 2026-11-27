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

"""Unit tests for W4 auto-launching the target app from Launcher / another screen."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from artemis.agents.flash.workflow_hook import FlashWorkflowHook
from artemis.workflow.recorder import RawScreen, RecordedStep, record_run
from artemis.workflow.store import WorkflowStore


PKG_CLOCK = "com.google.android.deskclock"
PKG_LAUNCHER = "com.google.android.apps.nexuslauncher"


def _n(pkg: str, rid: str, l: int, t: int, r: int, b: int, *, text: str = "", cls: str = "android.widget.Button", clickable: bool = True, editable: bool = False) -> dict:
    return {
        "resource-id": f"{pkg}:id/{rid}" if rid else "",
        "class": cls,
        "bounds": f"[{l},{t}][{r},{b}]",
        "clickable": str(clickable).lower(),
        "editable": str(editable).lower(),
        "enabled": "true",
        "visible-to-user": "true",
        "package": pkg,
        "text": text,
    }


def _launcher() -> list[dict]:
    return [
        _n(PKG_LAUNCHER, "workspace", 0, 0, 1000, 900, text="Home", clickable=False),
        _n(PKG_LAUNCHER, "icon1", 100, 200, 250, 350, text="Clock"),
        _n(PKG_LAUNCHER, "icon2", 300, 200, 450, 350, text="Chrome"),
        _n(PKG_LAUNCHER, "icon3", 500, 200, 650, 350, text="Contacts"),
        _n(PKG_LAUNCHER, "dock", 0, 900, 1000, 1000, text="Search"),
    ]


def _clock_home() -> list[dict]:
    return [
        _n(PKG_CLOCK, "title", 20, 20, 300, 80, text="Clock", cls="android.widget.TextView", clickable=False),
        _n(PKG_CLOCK, "fab", 400, 700, 600, 850, text="Add city"),
        _n(PKG_CLOCK, "tab_alarm", 20, 900, 300, 980, text="Alarm"),
        _n(PKG_CLOCK, "tab_clock", 350, 900, 650, 980, text="Clock", clickable=False),
        _n(PKG_CLOCK, "cities", 20, 150, 980, 650, text="Tokyo", cls="android.widget.TextView", clickable=False),
    ]


def _clock_search(typed: str = "") -> list[dict]:
    return [
        _n(PKG_CLOCK, "edit", 100, 40, 900, 120, text=typed, cls="android.widget.EditText", editable=True),
        _n(PKG_CLOCK, "clear", 900, 40, 980, 120, text="Clear"),
        _n(PKG_CLOCK, "empty", 200, 300, 800, 400, text="Search for a city", cls="android.widget.TextView", clickable=False),
        _n(PKG_CLOCK, "go", 850, 850, 990, 930, text="Search"),
        _n(PKG_CLOCK, "back", 10, 40, 90, 120, text="Back"),
    ]


def _clock_results() -> list[dict]:
    return [
        _n(PKG_CLOCK, "rv", 0, 150, 1000, 900, text="", clickable=False),
        _n(PKG_CLOCK, "city_name", 50, 180, 950, 260, text="Dublin, Ireland", clickable=True),
        _n(PKG_CLOCK, "city_time", 50, 280, 950, 360, text="9:00 AM", clickable=False),
        _n(PKG_CLOCK, "back", 10, 40, 90, 120, text="Back"),
        _n(PKG_CLOCK, "clear", 900, 40, 980, 120, text="Clear"),
    ]


def _seed_clock_workflow(store_dir) -> None:
    s1 = RawScreen(_clock_home(), 1000, 1000, seq=1)
    s2 = RawScreen(_clock_search(), 1000, 1000, seq=2)
    s3 = RawScreen(_clock_search("Madrid"), 1000, 1000, seq=3)
    s4 = RawScreen(_clock_results(), 1000, 1000, seq=4)
    steps = [
        RecordedStep(
            tool="click",
            args={"target": 1},
            action={"normalized_coordinates": [500, 775], "target_bounds": [400, 700, 600, 850], "target_resource_id": f"{PKG_CLOCK}:id/fab"},
            pre=s1,
            post=s2,
        ),
        RecordedStep(
            tool="input_text",
            args={"text": "Madrid"},
            action={"normalized_coordinates": [500, 80], "target_bounds": [100, 40, 900, 120]},
            pre=s2,
            post=s3,
        ),
        RecordedStep(
            tool="click",
            args={"target": 4},
            action={"normalized_coordinates": [920, 890], "target_bounds": [850, 850, 990, 930], "target_resource_id": f"{PKG_CLOCK}:id/go"},
            pre=s3,
            post=s4,
        ),
    ]
    wf = record_run(steps, goal="Open the Clock app and add city Madrid")
    assert wf is not None
    WorkflowStore(store_dir).save(wf)


def test_launcher_auto_opens_target_app_and_replays_without_ai_turn(tmp_path):
    _seed_clock_workflow(tmp_path)
    dispatched: list[tuple[str, dict]] = []
    shell_calls = 0
    ctx = SimpleNamespace(
        last_raw_observation={"elements": _launcher(), "width": 1000, "height": 1000, "seq": 1},
        device=SimpleNamespace(device_id="emulator-5554"),
    )

    async def fake_shell(cmd: str) -> str:
        nonlocal shell_calls
        shell_calls += 1
        assert "resolve-activity" in cmd and "category.HOME" in cmd
        return f"ActivityInfo:\n  packageName={PKG_LAUNCHER}\n"

    class FakeTurn:
        def __init__(self):
            self.actions = []
            self.had_no_effect = False
            self.had_ineffective_tap = False

    async def fake_exec(tool, args, tc_id, state, history, label, extra, b, x, seq, turn, index_elements=None):
        dispatched.append((tool, dict(args)))
        if tool == "manage_app":
            ctx.last_raw_observation = {"elements": _clock_home(), "width": 1000, "height": 1000, "seq": seq + 1}
        elif tool == "click" and args.get("target") == 1 and not dispatched[-1][1].get("text"):
            if len([t for t, _ in dispatched if t == "click"]) == 1:
                ctx.last_raw_observation = {"elements": _clock_search(), "width": 1000, "height": 1000, "seq": seq + 1}
            else:
                ctx.last_raw_observation = {"elements": _clock_results(), "width": 1000, "height": 1000, "seq": seq + 1}
        elif tool == "input_text":
            ctx.last_raw_observation = {"elements": _clock_search(args["text"]), "width": 1000, "height": 1000, "seq": seq + 1}
        turn.actions.append((tool, "success", "ok"))
        return b"new", [], seq + 1

    async def fake_reobserve(state):
        return b"final", []

    runner = SimpleNamespace(
        ctx=ctx,
        goal="Open the Clock app and add city Dublin",
        _llm=None,
        _force_image=False,
        executor=SimpleNamespace(
            actuator=SimpleNamespace(controller=SimpleNamespace(_driver=SimpleNamespace(execute_shell=fake_shell)))
        ),
        _execute_and_record_action=fake_exec,
        _reobserve_after_resume=fake_reobserve,
    )

    import artemis.agents.flash.runner as flash_runner_mod

    orig_turn = getattr(flash_runner_mod, "_TurnRecord", None)
    flash_runner_mod._TurnRecord = FakeTurn
    try:
        hook = FlashWorkflowHook(runner, "replay", str(tmp_path))
        res = asyncio.run(hook.maybe_autopilot(SimpleNamespace(screen_scrollable=False), b"img", []))
    finally:
        if orig_turn is not None:
            flash_runner_mod._TurnRecord = orig_turn

    assert res is not None
    assert shell_calls == 1
    assert [t for t, _ in dispatched] == ["manage_app", "click", "input_text", "click"]
    assert dispatched[0][1] == {"action": "launch", "app_name": PKG_CLOCK}
    assert dispatched[2][1]["text"] == "Dublin"
    assert hook.selector_calls == 0


def test_launcher_does_not_open_any_app_when_goal_does_not_match(tmp_path):
    _seed_clock_workflow(tmp_path)
    dispatched: list[str] = []
    ctx = SimpleNamespace(
        last_raw_observation={"elements": _launcher(), "width": 1000, "height": 1000, "seq": 1},
        launcher_package=PKG_LAUNCHER,
    )

    async def fake_ask(_prompt: str) -> str:
        return '{"workflow": null, "slots": {}}'

    async def fake_exec(tool, *a, **kw):
        dispatched.append(tool)
        return b"", [], 1

    runner = SimpleNamespace(
        ctx=ctx,
        goal="Open Settings and enable Bluetooth",
        _llm=None,
        _force_image=False,
        _execute_and_record_action=fake_exec,
    )
    hook = FlashWorkflowHook(runner, "replay", str(tmp_path))
    hook._ask_text = fake_ask
    res = asyncio.run(hook.maybe_autopilot(SimpleNamespace(screen_scrollable=False), b"img", []))
    assert res is None
    assert dispatched == []
    assert hook.workflow is None
