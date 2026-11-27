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

"""Unit tests for W3 tap-based slots and selected/checked fingerprint state."""

from __future__ import annotations

import asyncio

from artemis.workflow.autopilot import Autopilot
from artemis.workflow.fingerprint import app_fingerprint, normalize
from artemis.workflow.matcher import match_screen
from artemis.workflow.recorder import RawScreen, RecordedStep, detect_slots, record_run
from artemis.workflow.store import Edge, Node, Workflow
from artemis.workflow.targets import TargetDescriptor


PKG = "com.example.shop"


def _n(
    rid: str,
    l: int,
    t: int,
    r: int,
    b: int,
    *,
    text: str = "",
    cls: str = "android.widget.Button",
    clickable: bool = True,
    selected: bool = False,
    checked: bool = False,
    checkable: bool = False,
) -> dict:
    return {
        "resource-id": f"{PKG}:id/{rid}" if rid else "",
        "class": cls,
        "bounds": f"[{l},{t}][{r},{b}]",
        "clickable": str(clickable).lower(),
        "selected": str(selected).lower(),
        "checked": str(checked).lower(),
        "checkable": str(checkable).lower(),
        "enabled": "true",
        "visible-to-user": "true",
        "package": PKG,
        "text": text,
    }


def _tabs_screen(active: str) -> list[dict]:
    return [
        _n("header", 20, 20, 400, 80, text="Shop", cls="android.widget.TextView", clickable=False),
        _n("body", 20, 120, 980, 800, text="Content", cls="android.widget.TextView", clickable=False),
        _n("tab_home", 0, 900, 330, 980, text="Home", selected=(active == "Home")),
        _n("tab_timer", 330, 900, 660, 980, text="Timer", selected=(active == "Timer")),
        _n("tab_cart", 660, 900, 1000, 980, text="Cart", selected=(active == "Cart")),
    ]


def _options_screen(chosen: str = "") -> list[dict]:
    return [
        _n("hdr", 20, 20, 400, 80, text="Pick size", cls="android.widget.TextView", clickable=False),
        _n("opt_s", 20, 150, 300, 230, text="size S", cls="android.widget.RadioButton", checkable=True, checked=(chosen == "size S")),
        _n("opt_m", 340, 150, 620, 230, text="size M", cls="android.widget.RadioButton", checkable=True, checked=(chosen == "size M")),
        _n("opt_l", 660, 150, 940, 230, text="size L", cls="android.widget.RadioButton", checkable=True, checked=(chosen == "size L")),
        _n("ok_btn", 400, 800, 600, 880, text="OK"),
        _n("one_btn", 100, 800, 250, 880, text="1"),
    ]


def _done_screen() -> list[dict]:
    return [
        _n("done_t", 20, 20, 600, 100, text="Added", cls="android.widget.TextView", clickable=False),
        _n("d1", 20, 200, 400, 300, text="Summary", cls="android.widget.TextView", clickable=False),
        _n("d2", 20, 320, 400, 420, text="Total", cls="android.widget.TextView", clickable=False),
        _n("d3", 20, 440, 400, 540, text="Next", cls="android.widget.TextView", clickable=False),
        _n("d4", 20, 560, 400, 660, text="Back"),
    ]


def test_detect_slots_captures_tap_option_and_rejects_short_generic_and_partial():
    scr = RawScreen(_options_screen(), 1000, 1000, seq=1)
    done = RawScreen(_done_screen(), 1000, 1000, seq=2)

    steps = [
        RecordedStep(
            tool="click",
            args={"target": 4},
            action={"normalized_coordinates": [800, 190], "target_bounds": [660, 150, 940, 230], "target_resource_id": f"{PKG}:id/opt_l"},
            pre=scr,
            post=done,
        ),
        RecordedStep(
            tool="click",
            args={"target": 5},
            action={"normalized_coordinates": [500, 840], "target_bounds": [400, 800, 600, 880], "target_resource_id": f"{PKG}:id/ok_btn"},
            pre=scr,
            post=done,
        ),
        RecordedStep(
            tool="click",
            args={"target": 6},
            action={"normalized_coordinates": [175, 840], "target_bounds": [100, 800, 250, 880], "target_resource_id": f"{PKG}:id/one_btn"},
            pre=scr,
            post=done,
        ),
    ]
    slots = detect_slots(steps, "Pick 1 cup size L then OK")
    assert slots == {"slot1": "size L"}

    # Whole-word check: "Timer" inside "Timers" must not match as whole word.
    tab_scr = RawScreen(_tabs_screen("Home"), 1000, 1000, seq=1)
    tab_step = RecordedStep(
        tool="click",
        args={"target": 4},
        action={"normalized_coordinates": [495, 940], "target_bounds": [330, 900, 660, 980], "target_resource_id": f"{PKG}:id/tab_timer"},
        pre=tab_scr,
        post=done,
    )
    assert detect_slots([tab_step], "Check Timers list") == {}
    assert detect_slots([tab_step], "Open tab Timer now") == {"slot1": "Timer"}

    # Known nav edge in workflow must not become a slot.
    existing_wf = Workflow(
        "w1",
        PKG,
        "old",
        edges=[Edge("e1", "n1", "n2", "nav", target=TargetDescriptor(resource_id=f"{PKG}:id/tab_timer", text="Timer"))],
    )
    assert detect_slots([tab_step], "Open tab Timer now", workflow=existing_wf) == {}


def test_tap_slot_replays_for_another_option():
    scr = RawScreen(_options_screen(), 1000, 1000, seq=1)
    done = RawScreen(_done_screen(), 1000, 1000, seq=2)
    step = RecordedStep(
        tool="click",
        args={"target": 4},
        action={"normalized_coordinates": [800, 190], "target_bounds": [660, 150, 940, 230], "target_resource_id": f"{PKG}:id/opt_l"},
        pre=scr,
        post=done,
    )
    wf = record_run([step], goal="Order tea size L")
    assert wf is not None and wf.slots == {"slot1": "size L"}
    assert wf.edges[0].kind == "slot"

    class FakeOptApp:
        def __init__(self):
            self.screen = _options_screen()
            self.clicked = []

        def current(self):
            return RawScreen(self.screen, 1000, 1000, seq=1)

        async def refresh(self):
            return self.current()

        async def act(self, tool, args, element):
            self.clicked.append((tool, element.get("text")))
            self.screen = _done_screen()
            return True, "ok"

    app = FakeOptApp()
    rep = asyncio.run(Autopilot(wf, {"slot1": "size M"}, poll_interval_s=0).run(app))
    assert rep.steps == 1
    assert app.clicked == [("click", "size M")]


def test_selected_tab_state_distinguishes_identical_tab_layouts():
    fp_home = app_fingerprint(normalize(_tabs_screen("Home"), width=1000, height=1000))
    fp_timer = app_fingerprint(normalize(_tabs_screen("Timer"), width=1000, height=1000))
    fp_cart = app_fingerprint(normalize(_tabs_screen("Cart"), width=1000, height=1000))

    assert fp_home.tokens != fp_timer.tokens
    res = match_screen(fp_cart, {"home": [fp_home], "timer": [fp_timer]})
    assert not res.matched
    res2 = match_screen(fp_home, {"home": [fp_home], "timer": [fp_timer]})
    assert res2.matched and res2.node_id == "home"

