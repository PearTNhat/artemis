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

"""Regression tests for L1, L2, L3, L4 and app-name slot filtering."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from artemis.agents.flash.workflow_hook import FlashWorkflowHook
from artemis.mcp.action_executor import McpActionExecutor
from artemis.utils.coordinates import compute_smart_swipe_coordinates
from artemis.workflow.autopilot import Autopilot
from artemis.workflow.fingerprint import normalize
from artemis.workflow.recorder import RawScreen, RecordedStep, detect_slots, record_run
from artemis.workflow.store import Edge, Node, Workflow, WorkflowStore
from artemis.workflow.targets import TargetDescriptor, is_list_item, resolve
from scripts.workflow_replay_eval import _OfflineTraceDriver, _matches_ground_truth


def _node(
    pkg: str,
    rid: str,
    l: int,
    t: int,
    r: int,
    b: int,
    *,
    text: str = "",
    cls: str = "android.widget.TextView",
    clickable: bool = False,
    scrollable: bool = False,
    editable: bool = False,
) -> dict:
    return {
        "resource-id": f"{pkg}:id/{rid}" if rid else "",
        "class": cls,
        "bounds": f"[{l},{t}][{r},{b}]",
        "clickable": str(clickable).lower(),
        "scrollable": str(scrollable).lower(),
        "editable": str(editable).lower(),
        "enabled": "true",
        "visible-to-user": "true",
        "package": pkg,
        "text": text,
    }


def test_l4_smart_swipe_avoids_floating_fab_and_keyboard():
    pkg = "com.google.android.deskclock"
    # DeskClock home: RecyclerView covers [0,150][720,1080], FAB sits right on [600, 700] (normalized [367,662,633,812]).
    raw_tree = [
        _node(pkg, "cities", 0, 150, 720, 1080, cls="androidx.recyclerview.widget.RecyclerView", scrollable=True),
        _node(pkg, "city_name", 60, 400, 200, 460, text="Tokyo"),
        _node(pkg, "fab", 264, 847, 456, 1039, text="Add city", cls="android.widget.Button", clickable=True),
    ]
    sx, sy, ex, ey, _ = compute_smart_swipe_coordinates(
        direction="up",
        ui_hierarchy=raw_tree,
        width=720,
        height=1280,
    )
    # Start point must NOT fall inside the FAB [264..456, 847..1039]
    assert not (264 <= sx <= 456 and 847 <= sy <= 1039)
    assert 150 <= sy <= 1080 and ey < sy

    # Through McpActionExecutor._translate("swipe", {"direction": "up"}, ...) using ctx.last_raw_observation:
    ctx = SimpleNamespace(
        last_raw_observation={"elements": raw_tree, "width": 720, "height": 1280, "seq": 1},
        actuator=SimpleNamespace(extensions=lambda: [], controller=SimpleNamespace(width=720, height=1280)),
    )
    ex_inst = McpActionExecutor(ctx=ctx, actuator=ctx.actuator)
    ex_inst._screen_size = lambda: (720, 1280)
    _, wire_args, _, _ = ex_inst._translate("swipe", {"direction": "up"}, SimpleNamespace(indexed_elements=[]))
    nsx, nsy = wire_args["start"]
    assert not (367 <= nsx <= 633 and 662 <= nsy <= 812)


def test_l3_grab_wrap_content_branch_names_inside_recyclerview_are_list_items():
    pkg = "com.grabtaxi.passenger"
    # Case 1: short non-scrolling autocomplete RecyclerView (scrollable=False) with different wrap_content widths.
    short_list = [
        _node(pkg, "search_bar", 100, 40, 900, 120, text="Phở", cls="android.widget.EditText", editable=True),
        _node(pkg, "back_btn", 10, 40, 90, 120, text="Back", cls="android.widget.ImageButton", clickable=True),
        _node(pkg, "deliveries_search_autocomplete", 0, 172, 1000, 971, cls="androidx.recyclerview.widget.RecyclerView", scrollable=False),
        _node(pkg, "", 0, 172, 1000, 239, cls="android.view.View", clickable=True),
        _node(pkg, "", 163, 191, 652, 215, text="Phở Hồ Tây 978 Kha Vạn Cân"),
        _node(pkg, "", 163, 258, 385, 282, text="Phở Hồ Tây 2"),
    ]
    norm_short = normalize(short_list, width=1000, height=1000)
    branch1 = next(n for n in norm_short if n["text"] == "Phở Hồ Tây 978 Kha Vạn Cân")
    assert is_list_item(branch1, norm_short) is True

    # Recording a tap on that branch must become judgement, never nav.
    other_scr = RawScreen([_node(pkg, f"o{i}", 10, i * 100, 500, i * 100 + 60, text=f"Detail {i}") for i in range(5)], 1000, 1000, seq=2)
    step_branch = RecordedStep(
        tool="click",
        args={"target": 1},
        action={"normalized_coordinates": [300, 200], "target_bounds": [163, 191, 652, 215]},
        pre=RawScreen(short_list, 1000, 1000, seq=1),
        post=other_scr,
    )
    # Include a nav step so record_run returns a Workflow (not None) to verify classification.
    nav_step = RecordedStep(
        tool="click",
        args={"target": 1},
        action={"normalized_coordinates": [200, 50], "target_bounds": [10, 0, 500, 60], "target_resource_id": f"{pkg}:id/o0"},
        pre=other_scr,
        post=RawScreen([_node(pkg, f"p{i}", 10, i * 100, 500, i * 100 + 60, text=f"Cart {i}") for i in range(5)], 1000, 1000, seq=3),
    )
    wf = record_run([step_branch, nav_step], goal="Tìm phở bò")
    assert wf is not None
    assert wf.edges[0].kind == "judgement"

    # Case 2: wrap_content restaurant branches with slot value inside RecyclerView must return list_choice, not auto-pick.
    res_list = [
        _node(pkg, "deliveries_search_result", 0, 224, 1000, 971, cls="androidx.recyclerview.widget.RecyclerView", scrollable=True),
        _node(pkg, "", 355, 408, 668, 431, text="Ad   Phở Ngon 24h"),
        _node(pkg, "", 362, 634, 792, 658, text="Phở 24 - The Sun Avenue"),
    ]
    norm_res = normalize(res_list, width=1000, height=1000)
    _, status = resolve(TargetDescriptor(slot="slot1"), norm_res, slot_value="Phở 24")
    assert status == "list_choice"


def test_l2_w4_never_leaves_open_app_and_never_calls_llm_at_launcher(tmp_path):
    pkg_clock = "com.google.android.deskclock"
    pkg_chrome = "com.android.chrome"
    pkg_launcher = "com.google.android.apps.nexuslauncher"

    wf = Workflow(
        workflow_id=f"{pkg_clock}__1",
        package=pkg_clock,
        goal="Add city Madrid",
        slots={"slot1": "Madrid"},
        nodes={"n1": Node("n1")},
        edges=[Edge("e1", "n1", "n1", "slot", tool="input_text", slot="slot1")],
    )
    WorkflowStore(tmp_path).save(wf)

    chrome_nodes = [_node(pkg_chrome, f"c{i}", 10, i * 100, 500, i * 100 + 60, text=f"Chrome {i}") for i in range(5)]
    dispatched: list[str] = []
    llm_calls = 0

    async def ask(_p: str) -> str:
        nonlocal llm_calls
        llm_calls += 1
        return '{"workflow": 1, "slots": {"slot1": "Dublin"}}'

    # Case 1: Foreground is Chrome -> must NEVER switch to Clock!
    runner1 = SimpleNamespace(
        ctx=SimpleNamespace(last_raw_observation={"elements": chrome_nodes, "width": 1000, "height": 1000, "seq": 1}),
        goal="Add city Dublin",
        _llm=None,
        _execute_and_record_action=lambda *a, **kw: dispatched.append(a[0]),
    )
    hook1 = FlashWorkflowHook(runner1, "replay", str(tmp_path))
    hook1._ask_text = ask
    assert asyncio.run(hook1.maybe_autopilot(SimpleNamespace(screen_scrollable=False), b"img", [])) is None
    assert dispatched == []

    # Case 2: At Launcher, rephrased goal (not exact template match) -> must NOT call LLM!
    launcher_nodes = [_node(pkg_launcher, f"l{i}", 10, i * 100, 500, i * 100 + 60, text=f"App {i}") for i in range(5)]
    runner2 = SimpleNamespace(
        ctx=SimpleNamespace(
            last_raw_observation={"elements": launcher_nodes, "width": 1000, "height": 1000, "seq": 1},
            launcher_package=pkg_launcher,
        ),
        goal="Please put Dublin into my clock",
        _llm=None,
        _execute_and_record_action=lambda *a, **kw: dispatched.append(a[0]),
    )
    hook2 = FlashWorkflowHook(runner2, "replay", str(tmp_path))
    hook2._ask_text = ask
    assert asyncio.run(hook2.maybe_autopilot(SimpleNamespace(screen_scrollable=False), b"img", [])) is None
    assert llm_calls == 0
    assert dispatched == []


def test_l1_w0_flags_typing_into_wrong_editable_field_as_mismatch():
    pkg = "com.example.contacts"
    scr = RawScreen(
        [
            _node(pkg, "first", 20, 100, 980, 180, text="", cls="android.widget.EditText", editable=True),
            _node(pkg, "last", 20, 220, 980, 300, text="", cls="android.widget.EditText", editable=True),
        ],
        1000,
        1000,
        seq=1,
    )
    gt_last = RecordedStep(
        tool="input_text",
        args={"text": "Tran"},
        action={"normalized_coordinates": [500, 260], "target_bounds": [20, 220, 980, 300]},
        pre=scr,
    )
    wrong_el = {"bounds": [20, 100, 980, 180], "resource-id": f"{pkg}:id/first"}
    assert _matches_ground_truth("input_text", {"text": "Tran"}, wrong_el, gt_last) is False


def test_l1_w0_matches_actual_edited_field_when_gt_target_was_non_editable_overlay():
    pkg = "com.android.chrome"
    url_bar = {**_node(pkg, "url_bar", 156, 42, 811, 120, text="about:blank", cls="android.widget.EditText", editable=True), "focused": "true"}
    kb_node = _node("com.google.android.inputmethod.latin", "gif", 275, 533, 425, 602, text="GIF", cls="android.widget.FrameLayout", clickable=True)
    pre = RawScreen([url_bar, kb_node], 1000, 1000, seq=1)
    post = RawScreen([{**_node(pkg, "url_bar", 156, 42, 811, 120, text="weather in London", cls="android.widget.EditText", editable=True), "focused": "true"}], 1000, 1000, seq=2)
    gt_step = RecordedStep(
        tool="input_text",
        args={"text": "weather in London"},
        action={"normalized_coordinates": [350, 567], "target_bounds": [275, 533, 425, 602]},
        pre=pre,
        post=post,
    )
    ap_url_bar = {"bounds": [156, 42, 811, 120], "resource-id": f"{pkg}:id/url_bar"}
    assert _matches_ground_truth("input_text", {"text": "weather in London"}, ap_url_bar, gt_step) is True


def test_l4_smart_swipe_avoids_keyboard_window():
    pkg = "com.example.contacts"
    kb = "com.google.android.inputmethod.latin"
    raw_tree = [
        _node(pkg, "form_scroll", 0, 120, 720, 1180, cls="android.widget.ScrollView", scrollable=True),
        _node(kb, "key_a", 20, 720, 90, 810, text="a", clickable=True),
        _node(kb, "key_space", 200, 1060, 520, 1160, text="space", clickable=True),
    ]
    sx, sy, ex, ey, _ = compute_smart_swipe_coordinates(
        direction="up",
        ui_hierarchy=raw_tree,
        width=720,
        height=1280,
    )
    assert sy < 720 and ey < sy


def test_nho_app_name_in_goal_does_not_become_tap_slot():
    pkg = "com.google.android.deskclock"
    scr = RawScreen(
        [
            _node(pkg, "tab_alarm", 0, 900, 330, 980, text="Alarm", cls="android.widget.Button", clickable=True),
            _node(pkg, "tab_clock", 330, 900, 660, 980, text="Clock", cls="android.widget.Button", clickable=True),
            _node(pkg, "tab_timer", 660, 900, 1000, 980, text="Timer", cls="android.widget.Button", clickable=True),
        ],
        1000,
        1000,
        seq=1,
    )
    launch_clock = RecordedStep(tool="manage_app", args={"action": "launch", "app_name": "Clock"})
    tap_clock = RecordedStep(
        tool="click",
        args={"target": 2},
        action={"normalized_coordinates": [495, 940], "target_bounds": [330, 900, 660, 980], "target_resource_id": f"{pkg}:id/tab_clock"},
        pre=scr,
    )
    # Arbitrary phrasing without "open the" still drops "Clock" because manage_app launched "Clock".
    slots = detect_slots([launch_clock, tap_clock], "Trong ứng dụng Clock, vào tab Clock rồi thêm thành phố Madrid")
    assert "Clock" not in slots.values()

    # Non-app tab name in the same sentence STILL becomes a slot.
    tap_timer = RecordedStep(
        tool="click",
        args={"target": 3},
        action={"normalized_coordinates": [830, 940], "target_bounds": [660, 900, 1000, 980], "target_resource_id": f"{pkg}:id/tab_timer"},
        pre=scr,
    )
    assert detect_slots([launch_clock, tap_timer], "Mở Clock rồi sang tab Timer") == {"slot1": "Timer"}

    # App opened by tapping its Launcher icon ("Đồng hồ") also excludes "Đồng hồ" in any Vietnamese phrasing.
    launcher_scr = RawScreen(
        [_node("com.custom.home", "app_clock", 100, 200, 250, 350, text="Đồng hồ", cls="android.widget.TextView", clickable=True)],
        1000,
        1000,
        seq=0,
    )
    scr_vi = RawScreen(
        [
            _node(pkg, "tab_alarm", 0, 900, 330, 980, text="Báo thức", cls="android.widget.Button", clickable=True),
            _node(pkg, "tab_clock", 330, 900, 660, 980, text="Đồng hồ", cls="android.widget.Button", clickable=True),
        ],
        1000,
        1000,
        seq=2,
    )
    tap_home_icon = RecordedStep(
        tool="click",
        args={"target": 1},
        action={"normalized_coordinates": [175, 275], "target_bounds": [100, 200, 250, 350], "target_text": "Đồng hồ"},
        pre=launcher_scr,
        post=scr_vi,
    )
    tap_vi_clock = RecordedStep(
        tool="click",
        args={"target": 2},
        action={"normalized_coordinates": [495, 940], "target_bounds": [330, 900, 660, 980], "target_resource_id": f"{pkg}:id/tab_clock"},
        pre=scr_vi,
    )
    assert detect_slots([tap_home_icon, tap_vi_clock], "Trong ứng dụng Đồng hồ chọn giờ") == {}


def test_l024_detects_custom_home_package_via_adb_and_caches_once(tmp_path):
    pkg_clock = "com.google.android.deskclock"
    pkg_custom_home = "com.custom.oem.desktopshell"  # intentionally no "launcher" or ".home"
    wf = Workflow(
        workflow_id=f"{pkg_clock}__1",
        package=pkg_clock,
        goal="Add city Madrid",
        slots={"slot1": "Madrid"},
        nodes={"n1": Node("n1")},
        edges=[Edge("e1", "n1", "n1", "slot", tool="input_text", slot="slot1")],
    )
    WorkflowStore(tmp_path).save(wf)

    home_nodes = [_node(pkg_custom_home, f"h{i}", 10, i * 100, 500, i * 100 + 60, text=f"Icon {i}") for i in range(5)]
    dispatched: list[tuple[str, dict]] = []
    shell_calls = 0

    async def fake_shell(cmd: str) -> str:
        nonlocal shell_calls
        shell_calls += 1
        assert "resolve-activity" in cmd and "category.HOME" in cmd
        return f"ActivityInfo:\n  packageName={pkg_custom_home}\n"

    async def fake_exec(tool, args, *a, **kw):
        dispatched.append((tool, dict(args)))
        return b"", [], 1

    ctx = SimpleNamespace(
        last_raw_observation={"elements": home_nodes, "width": 1000, "height": 1000, "seq": 1},
        device=SimpleNamespace(device_id="dev-oem-1"),
    )
    runner = SimpleNamespace(
        ctx=ctx,
        goal="Add city Dublin",
        _llm=None,
        executor=SimpleNamespace(
            actuator=SimpleNamespace(controller=SimpleNamespace(_driver=SimpleNamespace(execute_shell=fake_shell)))
        ),
        _execute_and_record_action=fake_exec,
    )
    hook = FlashWorkflowHook(runner, "replay", str(tmp_path))
    asyncio.run(hook.maybe_autopilot(SimpleNamespace(screen_scrollable=False), b"img", []))
    assert dispatched == [("manage_app", {"action": "launch", "app_name": pkg_clock})]
    # Second hook / turn on the same device must reuse the cached launcher package.
    hook2 = FlashWorkflowHook(runner, "replay", str(tmp_path))
    asyncio.run(hook2._launcher_package())
    assert shell_calls == 1


def test_l022_w0_advances_to_gt_post_on_wrong_tap_to_catch_undetected():
    pkg = "com.example.shop"
    scr_dish = RawScreen(
        [
            _node(pkg, "title", 20, 20, 900, 80, text="Combo 75K"),
            _node(pkg, "make_another", 20, 700, 980, 770, text="Make another", cls="android.widget.Button", clickable=True),
            _node(pkg, "update_basket", 20, 820, 980, 900, text="Update Basket", cls="android.widget.Button", clickable=True),
        ],
        1000,
        1000,
        seq=1,
    )
    scr_after = RawScreen(
        [
            _node(pkg, "rest_name", 20, 20, 900, 80, text="Pho 24"),
            _node(pkg, "basket_bar", 20, 900, 980, 980, text="Basket", cls="android.widget.Button", clickable=True),
        ],
        1000,
        1000,
        seq=2,
    )
    gt_update = RecordedStep(
        tool="click",
        args={"target": 3},
        action={"normalized_coordinates": [500, 860], "target_bounds": [20, 820, 980, 900], "target_resource_id": f"{pkg}:id/update_basket"},
        pre=scr_dish,
        post=scr_after,
    )
    drv = _OfflineTraceDriver([gt_update])
    ap_wrong_make_another = {"bounds": [20, 700, 980, 770], "resource-id": f"{pkg}:id/make_another"}
    ok, _ = asyncio.run(drv.act("click", {"target": 1}, ap_wrong_make_another))
    assert ok is True
    assert drv.mismatches == 1
    assert drv.current() == scr_after


def test_l026_commitment_workflow_needs_two_runs_for_nav_steps():
    pkg = "com.example.grab"
    header = [_node(pkg, f"h{i}", 10, 10 + i * 50, 900, 50 + i * 50, text=f"Header {i}") for i in range(4)]
    home_scr = RawScreen(
        header + [_node(pkg, "food", 20, 300, 200, 400, text="Food", clickable=True), _node(pkg, "mart", 250, 300, 430, 400, text="Mart", clickable=True)],
        1000,
        1000,
        seq=1,
    )
    sheet_scr = RawScreen(
        header
        + [
            _node(pkg, "dish_title", 20, 280, 800, 340, text="Pho Bo"),
            _node(pkg, "make_another", 20, 700, 980, 770, text="Make another", cls="android.widget.Button", clickable=True),
            _node(pkg, "add_btn", 20, 820, 980, 900, text="Add to Basket", cls="android.widget.Button", clickable=True),
        ],
        1000,
        1000,
        seq=2,
    )
    cart_scr = RawScreen(
        header
        + [
            _node(pkg, "total", 20, 700, 500, 760, text="Total 75.000"),
            _node(pkg, "place_order", 20, 840, 980, 940, text="Place Order", cls="android.widget.Button", clickable=True),
        ],
        1000,
        1000,
        seq=3,
    )

    def tap_food():
        return RecordedStep(
            tool="click",
            args={"target": 5},
            action={"normalized_coordinates": [110, 350], "target_bounds": [20, 300, 200, 400], "target_resource_id": f"{pkg}:id/food"},
            pre=home_scr,
            post=sheet_scr,
            after_user_choice=True,
        )

    run1 = [
        tap_food(),
        RecordedStep(
            tool="click",
            args={"target": 6},
            action={"normalized_coordinates": [500, 735], "target_bounds": [20, 700, 980, 770], "target_resource_id": f"{pkg}:id/make_another"},
            pre=sheet_scr,
            post=cart_scr,
        ),
        RecordedStep(
            tool="click",
            args={"target": 6},
            action={"normalized_coordinates": [500, 890], "target_bounds": [20, 840, 980, 940], "target_resource_id": f"{pkg}:id/place_order"},
            pre=cart_scr,
            post=cart_scr,
        ),
    ]
    wf = record_run(run1, goal="Order 1 pho")
    assert wf is not None
    make_another_edge = next(e for e in wf.edges if e.label == "Make another")
    assert make_another_edge.state == "provisional" and make_another_edge.seen == 1

    # On replay before confirmation, Autopilot refuses to auto-tap "Make another".
    class SheetApp:
        def __init__(self):
            self.screen = sheet_scr
            self.acted = False

        def current(self):
            return self.screen

        async def refresh(self):
            return self.screen

        async def act(self, tool, args, element):
            self.acted = True
            return True, "ok"

    rep1 = asyncio.run(Autopilot(wf, {}, poll_interval_s=0).run(SheetApp()))
    assert rep1.acted == 0 and "only seen once" in rep1.stop_reason

    # Run 2 clicks "Add to Basket" (never "Make another"), and Run 3 merges "Add to Basket" again -> confirmed!
    add_step = RecordedStep(
        tool="click",
        args={"target": 7},
        action={"normalized_coordinates": [500, 860], "target_bounds": [20, 820, 980, 900], "target_resource_id": f"{pkg}:id/add_btn"},
        pre=sheet_scr,
        post=cart_scr,
    )
    record_run([tap_food(), add_step], goal="Order 1 pho", workflow=wf, slot_values={})
    assert next(e for e in wf.edges if e.label == "Add to Basket").state == "provisional"
    record_run([tap_food(), add_step], goal="Order 1 pho", workflow=wf, slot_values={})

    add_edge = next(e for e in wf.edges if e.label == "Add to Basket")
    assert add_edge.state == "active" and add_edge.seen == 2
    assert make_another_edge.state == "provisional" and make_another_edge.seen == 1

    class ConfirmedSheetApp(SheetApp):
        def __init__(self):
            super().__init__()
            self.clicked_rid = ""

        async def act(self, tool, args, element):
            self.acted = True
            self.clicked_rid = (element or {}).get("resource-id") or ""
            self.screen = cart_scr
            return True, "ok"

    capp = ConfirmedSheetApp()
    rep2 = asyncio.run(Autopilot(wf, {}, poll_interval_s=0).run(capp))
    assert rep2.steps == 1 and capp.clicked_rid.endswith("add_btn")


def test_workflow_ui_telemetry_publishes_session_and_step_markers(tmp_path):
    from unittest.mock import MagicMock
    from artemis.agents.flash.runner import FlashRunner
    from artemis.utils.timing_report import build_timing_report
    import sqlite3

    updated: list[dict] = []
    de = SimpleNamespace(
        update_session_device_info=lambda **kw: updated.append(kw),
        current_step_id="step-1",
        allocate_step_id=lambda: "step-1",
    )
    wf = Workflow(workflow_id="com.example.app__1", package="com.example.app", goal="Test", slots={"slot1": "x"})
    runner = SimpleNamespace(ctx=SimpleNamespace(data_engine=de), goal="Test")
    hook = FlashWorkflowHook(runner, "replay", str(tmp_path))
    assert updated and updated[-1]["workflow"]["mode"] == "replay"
    hook.workflow = wf
    hook.slot_values = {"slot1": "x"}
    hook.autopilot_steps = 2
    hook._publish_session_workflow(saved=True)
    assert updated[-1]["workflow"] == {
        "mode": "replay",
        "autopilot_steps": 2,
        "selector_calls": 0,
        "workflow_id": "com.example.app__1",
        "package": "com.example.app",
        "slots": {"slot1": "x"},
        "saved": True,
    }

    # Verify FlashRunner._record_action_step stamps workflow_autopilot & workflow_id into extra_metadata.
    captured_meta: list[dict] = []
    de.record_step = lambda **kw: captured_meta.append(kw["extra_metadata"]) or "step-1"
    fake_runner = FlashRunner.__new__(FlashRunner)
    fake_runner.ctx = SimpleNamespace(data_engine=de, transcript_ledger=None)
    fake_runner._wf = SimpleNamespace(_in_autopilot=True, mode="replay", workflow=wf)
    fake_runner._step_timing = lambda _res: {"action_s": 0.2}
    fake_runner._build_action_record = lambda n, a, r: {"action": n, "args": a}
    exec_res = SimpleNamespace(status="success", text_summary="ok", ui_elements_text=[], metadata={})
    fake_runner._record_action_step("click", {"target": 1}, exec_res, "[autopilot] workflow com.example.app__1", {}, b"", [], b"")
    assert captured_meta[0]["workflow_autopilot"] is True
    assert captured_meta[0]["workflow_id"] == "com.example.app__1"

    # Verify build_timing_report surfaces autopilot=True on workflow-replayed steps.
    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sessions (session_id TEXT, start_time REAL, initial_goal TEXT, status TEXT, device_info TEXT)")
    conn.execute("CREATE TABLE steps (session_id TEXT, step_number INTEGER, timestamp REAL, action_taken TEXT, extra_metadata TEXT)")
    conn.execute("CREATE TABLE traces (session_id TEXT, type TEXT, name TEXT, duration REAL)")
    conn.execute("INSERT INTO sessions VALUES ('s1', 100.0, 'g', 'completed', '{}')")
    conn.execute(
        "INSERT INTO steps VALUES ('s1', 1, 101.0, '{\"action\":\"click\",\"autopilot\":true,\"workflow_id\":\"com.example.app__1\"}', '{\"workflow_autopilot\":true,\"workflow_id\":\"com.example.app__1\"}')"
    )
    conn.commit()
    conn.close()
    report = build_timing_report(db_path, "s1")
    assert report is not None and report["steps"][0]["autopilot"] is True and report["steps"][0]["workflow_id"] == "com.example.app__1"



def test_l027_numeric_labels_are_data_not_navigation():
    from artemis.workflow.targets import is_data_label

    for data in ("2", "111.000", "75.000 ₫", "$4.99", "1/3", "-", "12:30"):
        if any(ch.isdigit() for ch in data):
            assert is_data_label(data), data
    for control in ("Add city", "+2h", "Make another", "Size L", "Đặt 2 suất", ""):
        assert not is_data_label(control), control


def test_l027_tap_on_a_quantity_is_never_replayed():
    """Grab a99efb27 / f58deac1: both recordings met the same leftover basket
    (quantity "2") and tapped it; it must not become an autopilot step even
    when seen in two runs, and an old workflow holding it is refused live."""
    from artemis.workflow.fingerprint import normalize
    from artemis.workflow.recorder import RawScreen, RecordedStep, record_run
    from artemis.workflow.targets import TargetDescriptor, descriptor_for, resolve

    pkg = "com.example.food"

    def n(rid, l, t, r, b, text="", clickable=True, cls="android.widget.TextView"):
        return {"package": pkg, "class": cls, "resource-id": f"{pkg}:id/{rid}", "text": text,
                "bounds": f"[{l},{t}][{r},{b}]", "clickable": "true" if clickable else "false",
                "visible-to-user": "true"}

    sheet = [n("root", 0, 0, 1000, 1000, clickable=False, cls="android.widget.FrameLayout"),
             n("title", 20, 20, 980, 80, "Combo", clickable=False),
             n("price", 20, 100, 400, 160, "111.000", clickable=False),
             n("tv_quantity", 860, 900, 970, 980, "2"),
             n("btn_add", 20, 900, 800, 980, "Add to basket")]
    after = sheet[:3] + [n("qty_picker", 20, 300, 980, 700, "Quantity")]
    goal = "add one Combo to the basket"
    steps = [RecordedStep(tool="click", args={"target": 4},
                          action={"action": "click", "normalized_coordinates": [915, 940]},
                          pre=RawScreen(sheet, 1000, 1000), post=RawScreen(after, 1000, 1000))]
    for _ in range(2):  # two independent runs with the same leftover basket
        wf = record_run(steps, goal=goal)
    assert wf is None or not any(e.kind in ("nav", "slot") and e.target and e.target.resource_id.endswith("tv_quantity")
                                 for e in wf.edges)
    node, status = resolve(TargetDescriptor(resource_id=f"{pkg}:id/tv_quantity"), normalize(sheet, width=1000, height=1000))
    assert node is None and status == "data_label"


def test_workflow_verified_is_reported_for_each_replayed_step():
    """The UI shows verified workflow steps apart from ones the AI had to take
    over: the autopilot reports the destination check of every step it ran."""
    import asyncio

    from tests.unit.workflow.test_autopilot import FakeApp, build_confirmed_wf, home, other_screen_same_app
    from artemis.workflow.autopilot import Autopilot

    class ReportingApp(FakeApp):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.verified: list[bool] = []

        def note_verified(self, ok):
            self.verified.append(ok)

    wf = build_confirmed_wf()
    good = ReportingApp(home())
    asyncio.run(Autopilot(wf, {"slot1": "bun cha"}, poll_interval_s=0).run(good))
    assert good.verified and all(good.verified)

    wf = build_confirmed_wf()
    bad = ReportingApp(home(), enter_leads_to=other_screen_same_app)
    asyncio.run(Autopilot(wf, {"slot1": "bun cha"}, poll_interval_s=0).run(bad))
    assert bad.verified[-1] is False and all(bad.verified[:-1])


def test_merge_step_extra_metadata_waits_for_a_step_written_later(tmp_path):
    import threading
    import time
    import uuid

    from artemis.data_engine.models import SessionMetadata, StepRecord
    from artemis.data_engine.storage import StorageManager

    storage = StorageManager(tmp_path / "data_engine.db", tmp_path)
    sid = str(uuid.uuid4())
    storage.create_session(SessionMetadata(session_id=sid, initial_goal="g", start_time=1.0))
    step_id = str(uuid.uuid4())
    assert storage.merge_step_extra_metadata(step_id, {"workflow_verified": True}) is False  # not written yet

    def write_later():
        time.sleep(0.3)
        storage.create_step(StepRecord(step_id=step_id, session_id=sid, step_number=1, timestamp=2.0,
                                       extra_metadata={"workflow_autopilot": True}))

    threading.Thread(target=write_later).start()
    for _ in range(20):  # what DataEngine.merge_step_extra_metadata does in background
        if storage.merge_step_extra_metadata(step_id, {"workflow_verified": False}):
            break
        time.sleep(0.1)
    steps = storage.get_steps(sid)
    meta = steps[0].extra_metadata
    assert meta == {"workflow_autopilot": True, "workflow_verified": False}


# --- L-029: a "successful" run that overwrote its own typing must not be learned ---

def _contact_form(first="", visible_last=True, focused_first=False):
    pkg = "com.example.contacts"

    def ed(hint, t, b, text="", focused=False):
        return {"package": pkg, "class": "android.widget.EditText", "resource-id": "", "text": text,
                "hint": hint, "bounds": f"[144,{t}][844,{b}]", "clickable": "true", "editable": "true",
                "focused": "true" if focused else "false", "visible-to-user": "true"}

    nodes = [{"package": pkg, "class": "android.widget.FrameLayout", "resource-id": f"{pkg}:id/root",
              "bounds": "[0,0][1000,1000]", "visible-to-user": "true"},
             {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/title",
              "text": "Create contact", "bounds": "[20,20][600,80]", "visible-to-user": "true"},
             {"package": pkg, "class": "android.widget.Button", "resource-id": f"{pkg}:id/save",
              "text": "Save", "bounds": "[800,20][980,80]", "clickable": "true", "visible-to-user": "true"},
             ed("First name", 441, 533, first, focused_first)]
    if visible_last:
        nodes.append(ed("Last name", 671, 762))
    return nodes


def test_l029_recorder_rejects_a_run_that_overwrote_its_own_typing():
    """Gemini c403e23a: "Tran" and the phone were typed at points hidden by the
    keyboard, landing in First name over "Minh"; the AI still said "completed"."""
    from artemis.workflow.recorder import RawScreen, RecordedStep, record_run

    R = lambda nodes: RawScreen(nodes, 1000, 1000)  # noqa: E731
    goal = "create a contact with first name Minh and last name Tran"
    steps = [
        RecordedStep(tool="input_text", args={"text": "Minh", "target": 4},
                     action={"action": "input_text", "normalized_coordinates": [494, 487]},
                     pre=R(_contact_form()), post=R(_contact_form("Minh", False, True))),
        RecordedStep(tool="input_text", args={"text": "Tran", "target": 12},  # point under the keyboard
                     action={"action": "input_text", "normalized_coordinates": [494, 716]},
                     pre=R(_contact_form("Minh", False, True)), post=R(_contact_form("Tran", False, True))),
        RecordedStep(tool="click", args={"target": 3},
                     action={"action": "click", "normalized_coordinates": [890, 50]},
                     pre=R(_contact_form("Tran", False, True)), post=R(_contact_form("Tran", False, True)[:2])),
    ]
    assert record_run(steps, goal=goal) is None


def test_l029_executor_refuses_typing_over_this_runs_own_value():
    from types import SimpleNamespace

    from artemis.mcp.action_executor import McpActionExecutor

    ex = McpActionExecutor.__new__(McpActionExecutor)
    ex.agent_name = "flash"
    ex._typed_values = ["minh"]
    ex.ctx = SimpleNamespace(last_raw_observation={"elements": _contact_form("Minh", False, True),
                                                   "width": 1000, "height": 1000})
    assert ex._input_would_go_elsewhere([494, 716]) == "First name"  # off any field, Minh would be lost
    assert ex._input_would_go_elsewhere([494, 487]) is None  # aimed at the field itself
    ex._typed_values = []
    assert ex._input_would_go_elsewhere([494, 716]) is None  # content this run did not type (a URL)
    ex.ctx.last_raw_observation["elements"] = _contact_form("", False, True)
    ex._typed_values = ["minh"]
    assert ex._input_would_go_elsewhere([494, 716]) is None  # empty focused field: nothing to overwrite


def test_l029_autopilot_never_types_over_a_value_it_typed_for_another_field():
    import asyncio

    from artemis.workflow.autopilot import Autopilot
    from artemis.workflow.recorder import RawScreen
    from artemis.workflow.store import Edge
    from artemis.workflow.targets import TargetDescriptor, descriptor_for

    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.store import Node, Workflow

    pkg = "com.example.contacts"
    fps = [app_fingerprint(normalize(_contact_form(t, False, bool(t)), width=1000, height=1000)) for t in ("", "Lan")]
    node = "n1"
    wf = Workflow(workflow_id="w", package=pkg, goal="create Lan Pham", nodes={node: Node(node, fps)})
    # a (wrong) workflow typing both values into the First name field
    wf.edges = [
        Edge(id="a", src=node, dst=node, kind="slot", tool="input_text", slot="slot1",
             target=TargetDescriptor(class_name="android.widget.EditText", hint="First name")),
        Edge(id="b", src=node, dst=node, kind="slot", tool="input_text", slot="slot2",
             target=TargetDescriptor(class_name="android.widget.EditText", hint="First name")),
    ]
    wf.slots = {"slot1": "Lan", "slot2": "Pham"}

    class FormApp:
        def __init__(self):
            self.first = ""
            self.actions = []

        def current(self):
            return RawScreen(_contact_form(self.first, False, bool(self.first)), 1000, 1000)

        async def refresh(self):
            return self.current()

        async def act(self, tool, args, element):
            self.actions.append((tool, args.get("text")))
            self.first = args.get("text", "")
            return True, "ok"

    app = FormApp()
    rep = asyncio.run(Autopilot(wf, {"slot1": "Lan", "slot2": "Pham"}, poll_interval_s=0).run(app))
    assert len(app.actions) == 1  # the second value is never typed over the first
    assert app.first in ("Lan", "Pham")
    assert "already holds" in rep.stop_reason


def test_l029_untargeted_typing_into_a_field_holding_this_runs_value_is_refused():
    """Gemini 4809e63a: input_text with no target went into the focused last-name
    field holding "Pham" -> saved "Lan 0912345678" without a phone number."""
    from types import SimpleNamespace

    from artemis.mcp.action_executor import McpActionExecutor

    ex = McpActionExecutor.__new__(McpActionExecutor)
    ex.agent_name = "flash"
    ex._typed_values = ["lan", "pham"]
    ex.ctx = SimpleNamespace(last_raw_observation={"elements": _contact_form("Pham", False, True),
                                                   "width": 1000, "height": 1000})
    assert ex._input_would_go_elsewhere(None) == "First name"
    ex.ctx.last_raw_observation["elements"] = _contact_form("", False, True)
    assert ex._input_would_go_elsewhere(None) is None  # empty focused field: fine


def test_l029_executor_short_and_whole_word_matching():
    from types import SimpleNamespace

    from artemis.mcp.action_executor import McpActionExecutor

    ex = McpActionExecutor.__new__(McpActionExecutor)
    ex.agent_name = "flash"
    ex._typed_values = ["1"]  # < 2 chars must not block URLs containing "1"
    ex.ctx = SimpleNamespace(
        last_raw_observation={"elements": _contact_form("https://a1.com", False, True), "width": 1000, "height": 1000}
    )
    assert ex._input_would_go_elsewhere([494, 716]) is None
    assert ex._input_would_go_elsewhere(None) is None

    # Substring inside another word/URL must not trigger either.
    ex._typed_values = ["a1"]
    assert ex._input_would_go_elsewhere([494, 716]) == "First name"  # "a1" IS a standalone token inside "https://a1.com"
    ex.ctx.last_raw_observation["elements"] = _contact_form("https://ba1x.com", False, True)
    assert ex._input_would_go_elsewhere([494, 716]) is None

    # Whole field / whole word ("Pham") still refused.
    ex._typed_values = ["pham"]
    ex.ctx.last_raw_observation["elements"] = _contact_form("Pham", False, True)
    assert ex._input_would_go_elsewhere([494, 716]) == "First name"
    assert ex._input_would_go_elsewhere(None) == "First name"


def test_l029_recorder_rejects_overwrite_then_scrolled_away_and_accepts_correction():
    from artemis.workflow.recorder import RawScreen, RecordedStep, record_run

    R = lambda nodes: RawScreen(nodes, 1000, 1000)  # noqa: E731
    goal = "create a contact with first name Minh and last name Tran"
    scrolled_away = [n for n in _contact_form("", True, False) if n.get("hint") != "First name"]
    overwrite_then_scroll = [
        RecordedStep(
            tool="input_text",
            args={"text": "Minh", "target": 4},
            action={"action": "input_text", "normalized_coordinates": [494, 487]},
            pre=R(_contact_form()),
            post=R(_contact_form("Minh", False, True)),
        ),
        RecordedStep(
            tool="input_text",
            args={"text": "Tran", "target": 12},
            action={"action": "input_text", "normalized_coordinates": [494, 716]},
            pre=R(_contact_form("Minh", False, True)),
            post=R(_contact_form("Tran", False, True)),
        ),
        RecordedStep(
            tool="swipe",
            args={"direction": "up"},
            action={"action": "swipe", "normalized_coordinates": [200, 700, 200, 300]},
            pre=R(_contact_form("Tran", False, True)),
            post=R(scrolled_away),
        ),
        RecordedStep(
            tool="click",
            args={"target": 3},
            action={"action": "click", "normalized_coordinates": [890, 50]},
            pre=R(scrolled_away),
            post=R(scrolled_away[:2]),
        ),
    ]
    assert record_run(overwrite_then_scroll, goal=goal) is None

    # Typed "Minh" wrongly into Last name, then fixed both fields -> still accepted!
    def form_both(first="", last=""):
        nodes = _contact_form(first, False, False)
        nodes.append(
            {
                "package": "com.example.contacts",
                "class": "android.widget.EditText",
                "resource-id": "",
                "text": last,
                "hint": "Last name",
                "bounds": "[144,671][844,762]",
                "clickable": "true",
                "editable": "true",
                "focused": "false",
                "visible-to-user": "true",
            }
        )
        return nodes

    corrected = [
        RecordedStep(
            tool="input_text",
            args={"text": "Minh", "target": 5},
            action={"action": "input_text", "normalized_coordinates": [494, 716]},
            pre=R(form_both()),
            post=R(form_both("", "Minh")),
        ),
        RecordedStep(
            tool="input_text",
            args={"text": "Tran", "target": 5},
            action={"action": "input_text", "normalized_coordinates": [494, 716]},
            pre=R(form_both("", "Minh")),
            post=R(form_both("", "Tran")),
        ),
        RecordedStep(
            tool="input_text",
            args={"text": "Minh", "target": 4},
            action={"action": "input_text", "normalized_coordinates": [494, 487]},
            pre=R(form_both("", "Tran")),
            post=R(form_both("Minh", "Tran")),
        ),
        RecordedStep(
            tool="click",
            args={"target": 3},
            action={"action": "click", "normalized_coordinates": [890, 50]},
            pre=R(form_both("Minh", "Tran")),
            post=R(form_both("Minh", "Tran")[:2]),
        ),
    ]
    assert record_run(corrected, goal=goal) is not None


def test_ok_runs_unchanged_when_rejected_by_inconsistent_typing(tmp_path):
    from artemis.workflow.recorder import RawScreen, RecordedStep

    R = lambda nodes: RawScreen(nodes, 1000, 1000)  # noqa: E731
    wf = Workflow(
        workflow_id="com.example.contacts__1",
        package="com.example.contacts",
        goal="create a contact with first name Minh and last name Tran",
        slots={"slot1": "Minh", "slot2": "Tran"},
        nodes={"n1": Node("n1")},
        edges=[Edge("e1", "n1", "n1", "slot", tool="input_text", slot="slot1")],
        runs=2,
        ok_runs=2,
    )
    WorkflowStore(tmp_path).save(wf)

    hook = FlashWorkflowHook(
        SimpleNamespace(ctx=SimpleNamespace(), goal="create a contact with first name Lan and last name Pham"),
        "replay",
        str(tmp_path),
    )
    hook.workflow = wf
    hook.slot_values = {"slot1": "Lan", "slot2": "Pham"}
    hook.steps = [
        RecordedStep(
            tool="input_text",
            args={"text": "Lan", "target": 4},
            action={"action": "input_text", "normalized_coordinates": [494, 487]},
            pre=R(_contact_form()),
            post=R(_contact_form("Lan", False, True)),
        ),
        RecordedStep(
            tool="input_text",
            args={"text": "Pham", "target": 12},
            action={"action": "input_text", "normalized_coordinates": [494, 716]},
            pre=R(_contact_form("Lan", False, True)),
            post=R(_contact_form("Pham", False, True)),
        ),
        RecordedStep(
            tool="click",
            args={"target": 3},
            action={"action": "click", "normalized_coordinates": [890, 50]},
            pre=R(_contact_form("Pham", False, True)),
            post=R(_contact_form("Pham", False, True)[:2]),
        ),
    ]
    asyncio.run(hook.finish({"status": "completed"}))
    reloaded = WorkflowStore(tmp_path).load("com.example.contacts", "com.example.contacts__1")
    assert reloaded is not None and reloaded.runs == 2 and reloaded.ok_runs == 2


def _make_99cd3b7b_screens():
    """Exact UI trees (720x1280) observed in session 99cd3b7b of data_engine.db."""
    from artemis.workflow.recorder import RawScreen

    R = lambda els: RawScreen(els, 720, 1280)  # noqa: E731
    list_pre = [
        {"package": "com.google.android.contacts", "class": "android.widget.EditText", "resource-id": "com.google.android.contacts:id/open_search_bar", "hint": "Search contacts", "bounds": "[32,64][688,160]", "clickable": "true", "scrollable": "false", "editable": "true", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.ListView", "resource-id": "android:id/list", "bounds": "[0,281][720,1072]", "clickable": "false", "scrollable": "true", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.TextView", "resource-id": "com.google.android.contacts:id/cliv_name_textview", "text": "Huy Do", "bounds": "[254,418][656,467]", "clickable": "false", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.TextView", "resource-id": "com.google.android.contacts:id/cliv_name_textview", "text": "Khoa Vu", "bounds": "[254,530][656,579]", "clickable": "false", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.ImageButton", "resource-id": "com.google.android.contacts:id/floating_action_button", "content-desc": "Create contact", "bounds": "[560,928][672,1040]", "clickable": "true", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
    ]
    form_initial = [
        {"package": "com.google.android.contacts", "class": "android.widget.ImageButton", "bounds": "[0,56][112,168]", "clickable": "true", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.TextView", "text": "Create contact", "bounds": "[120,80][418,144]", "clickable": "false", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.Button", "resource-id": "com.google.android.contacts:id/toolbar_button", "text": "Save", "bounds": "[448,64][624,160]", "clickable": "true", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.ScrollView", "resource-id": "com.google.android.contacts:id/contact_editor_scroller", "bounds": "[0,176][720,1232]", "clickable": "false", "scrollable": "true", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.TextView", "resource-id": "com.google.android.contacts:id/account_chip_prefix", "text": "Save to", "bounds": "[32,210][130,254]", "clickable": "false", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.Button", "resource-id": "com.google.android.contacts:id/account_chip", "text": "tuannhat626@gmail.com", "bounds": "[146,184][613,280]", "clickable": "true", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.Button", "resource-id": "com.google.android.contacts:id/button_change_photo", "text": "Add picture", "bounds": "[259,568][461,664]", "clickable": "true", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.EditText", "hint": "First name", "bounds": "[104,715][608,832]", "clickable": "true", "scrollable": "false", "editable": "true", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.EditText", "hint": "Last name", "bounds": "[104,859][608,976]", "clickable": "true", "scrollable": "false", "editable": "true", "focused": "false", "visible-to-user": "true"},
    ]

    def form_after_first(val: str = "Phuc"):
        return [
            {"package": "com.google.android.contacts", "class": "android.widget.ImageButton", "bounds": "[0,56][112,168]", "clickable": "true", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
            {"package": "com.google.android.contacts", "class": "android.widget.TextView", "text": "Create contact", "bounds": "[120,80][418,144]", "clickable": "false", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
            {"package": "com.google.android.contacts", "class": "android.widget.Button", "resource-id": "com.google.android.contacts:id/toolbar_button", "text": "Save", "bounds": "[448,64][624,160]", "clickable": "true", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
            {"package": "com.google.android.contacts", "class": "android.widget.ScrollView", "resource-id": "com.google.android.contacts:id/contact_editor_scroller", "bounds": "[0,176][720,682]", "clickable": "false", "scrollable": "true", "focused": "false", "visible-to-user": "true"},
            {"package": "com.google.android.contacts", "class": "android.widget.Button", "resource-id": "com.google.android.contacts:id/button_change_photo", "text": "Add picture", "bounds": "[259,418][461,514]", "clickable": "true", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
            {"package": "com.google.android.contacts", "class": "android.widget.EditText", "text": val, "hint": "First name", "bounds": "[104,565][608,682]", "clickable": "true", "scrollable": "false", "editable": "true", "focused": "true", "visible-to-user": "true"},
        ]

    form_scrolled_pre = [
        {"package": "com.google.android.contacts", "class": "android.widget.ImageButton", "bounds": "[0,56][112,168]", "clickable": "true", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.TextView", "text": "Create contact", "bounds": "[120,80][418,144]", "clickable": "false", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.Button", "resource-id": "com.google.android.contacts:id/toolbar_button", "text": "Save", "bounds": "[448,64][624,160]", "clickable": "true", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.ScrollView", "resource-id": "com.google.android.contacts:id/contact_editor_scroller", "bounds": "[0,176][720,1232]", "clickable": "false", "scrollable": "true", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.EditText", "hint": "Name prefix", "bounds": "[104,176][608,250]", "clickable": "true", "scrollable": "false", "editable": "true", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.EditText", "text": "Phuc", "hint": "First name", "bounds": "[104,277][608,394]", "clickable": "true", "scrollable": "false", "editable": "true", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.EditText", "hint": "Middle name", "bounds": "[104,421][608,538]", "clickable": "true", "scrollable": "false", "editable": "true", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.EditText", "text": "Ha", "hint": "Last name", "bounds": "[104,565][608,682]", "clickable": "true", "scrollable": "false", "editable": "true", "focused": "true", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.EditText", "hint": "Name suffix", "bounds": "[104,709][608,826]", "clickable": "true", "scrollable": "false", "editable": "true", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.EditText", "hint": "Company", "bounds": "[104,869][608,986]", "clickable": "true", "scrollable": "false", "editable": "true", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.EditText", "hint": "Phone", "bounds": "[104,1029][608,1146]", "clickable": "true", "scrollable": "false", "editable": "true", "focused": "false", "visible-to-user": "true"},
    ]

    def form_scrolled_post(last_val: str = "Ha", phone_val: str = "0903330003"):
        return [
            {"package": "com.google.android.contacts", "class": "android.widget.ImageButton", "bounds": "[0,56][112,168]", "clickable": "true", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
            {"package": "com.google.android.contacts", "class": "android.widget.TextView", "text": "Create contact", "bounds": "[120,80][418,144]", "clickable": "false", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
            {"package": "com.google.android.contacts", "class": "android.widget.Button", "resource-id": "com.google.android.contacts:id/toolbar_button", "text": "Save", "bounds": "[448,64][624,160]", "clickable": "true", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
            {"package": "com.google.android.contacts", "class": "android.widget.ScrollView", "resource-id": "com.google.android.contacts:id/contact_editor_scroller", "bounds": "[0,176][720,682]", "clickable": "false", "scrollable": "true", "focused": "false", "visible-to-user": "true"},
            {"package": "com.google.android.contacts", "class": "android.widget.EditText", "text": last_val, "hint": "Last name", "bounds": "[104,176][608,218]", "clickable": "true", "scrollable": "false", "editable": "true", "focused": "false", "visible-to-user": "true"},
            {"package": "com.google.android.contacts", "class": "android.widget.EditText", "hint": "Name suffix", "bounds": "[104,245][608,362]", "clickable": "true", "scrollable": "false", "editable": "true", "focused": "false", "visible-to-user": "true"},
            {"package": "com.google.android.contacts", "class": "android.widget.EditText", "hint": "Company", "bounds": "[104,405][608,522]", "clickable": "true", "scrollable": "false", "editable": "true", "focused": "false", "visible-to-user": "true"},
            {"package": "com.google.android.contacts", "class": "android.widget.EditText", "text": phone_val, "hint": "Phone", "bounds": "[104,565][608,682]", "clickable": "true", "scrollable": "false", "editable": "true", "focused": "true", "visible-to-user": "true"},
        ]

    detail_post = [
        {"package": "com.google.android.contacts", "class": "android.widget.ImageButton", "bounds": "[0,56][112,168]", "clickable": "true", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.Button", "resource-id": "com.google.android.contacts:id/menu_insert_or_edit", "bounds": "[432,64][528,160]", "clickable": "true", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.TextView", "resource-id": "com.google.android.contacts:id/large_title", "text": "Phuc Ha", "bounds": "[40,477][680,559]", "clickable": "false", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
        {"package": "com.google.android.contacts", "class": "android.widget.TextView", "resource-id": "com.google.android.contacts:id/header", "text": "0903330003", "bounds": "[144,959][335,1008]", "clickable": "false", "scrollable": "false", "focused": "false", "visible-to-user": "true"},
    ]
    return R, list_pre, form_initial, form_after_first, form_scrolled_pre, form_scrolled_post, detail_post


def test_l031_99cd3b7b_scrolled_and_initial_form_share_node_and_verify():
    from artemis.workflow.autopilot import Autopilot
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.matcher import DEFAULT_TAU, match_screen
    from artemis.workflow.recorder import RecordedStep, record_run
    from artemis.workflow.targets import TargetDescriptor, descriptor_for

    R, list_pre, form_initial, form_after_first, form_scrolled_pre, form_scrolled_post, detail_post = (
        _make_99cd3b7b_screens()
    )
    goal = "create contact Phuc Ha with phone 0903330003"
    slots = {"slot1": "Phuc", "slot2": "Ha", "slot3": "0903330003"}

    # Similarity between initial 99cd3b7b form and scrolled-down form is below tau=0.80.
    fp_init = app_fingerprint(normalize(form_initial, 720, 1280))
    fp_scrl = app_fingerprint(normalize(form_scrolled_post(), 720, 1280))
    assert not match_screen(fp_init, {"n2": [fp_scrl]}, tau=DEFAULT_TAU, delta=0.0).matched

    # 1) Recorder keeps initial, keyboard-open, and scrolled-down form states on ONE node.
    steps = [
        RecordedStep(
            tool="click",
            args={"target": 5},
            action={"action": "click", "normalized_coordinates": [856, 769], "target_text": "Create contact", "target_resource_id": "com.google.android.contacts:id/floating_action_button"},
            pre=R(list_pre),
            post=R(form_initial),
        ),
        RecordedStep(
            tool="input_text",
            args={"text": "Phuc", "target": 8},
            action={"action": "input_text", "normalized_coordinates": [494, 605]},
            pre=R(form_initial),
            post=R(form_after_first("Phuc")),
        ),
        RecordedStep(
            tool="input_text",
            args={"text": "Ha", "target": 9},
            action={"action": "input_text", "normalized_coordinates": [494, 716]},
            pre=R(form_initial),
            post=R(form_scrolled_pre),
        ),
        RecordedStep(
            tool="input_text",
            args={"text": "0903330003", "target": 11},
            action={"action": "input_text", "normalized_coordinates": [494, 849]},
            pre=R(form_scrolled_pre),
            post=R(form_scrolled_post()),
        ),
        RecordedStep(
            tool="click",
            args={"target": 3},
            action={"action": "click", "normalized_coordinates": [744, 88], "target_text": "Save", "target_resource_id": "com.google.android.contacts:id/toolbar_button"},
            pre=R(form_scrolled_post()),
            post=R(detail_post),
        ),
    ]
    wf = record_run(steps, goal=goal, slot_values=slots)
    assert wf is not None
    create_edge = next(e for e in wf.edges if e.target and e.target.text == "Create contact")
    save_edge = next(e for e in wf.edges if e.target and e.target.text == "Save")
    assert create_edge.dst == save_edge.src

    # 2) Autopilot verifies "Create contact" even when workflow only recorded the scrolled
    #    form fp on n2 (plus an unlinked stale initial node n4), without relaxing tau=0.80.
    fp_list = app_fingerprint(normalize(list_pre, 720, 1280))
    fp_after1 = app_fingerprint(normalize(form_after_first("Phuc"), 720, 1280))
    fp_detail = app_fingerprint(normalize(detail_post, 720, 1280))
    split_wf = Workflow(
        workflow_id="com.google.android.contacts__split",
        package="com.google.android.contacts",
        goal="create contact",
        slots=slots,
        nodes={
            "n1": Node("n1", [fp_list]),
            "n2": Node("n2", [fp_after1, fp_scrl]),
            "n4": Node("n4", [fp_init]),  # decoy initial node mimicking stale L-031 split
            "n5": Node("n5", [fp_detail]),
        },
        edges=[
            Edge(
                "e1",
                "n1",
                "n2",
                "nav",
                tool="click",
                target=TargetDescriptor(resource_id="com.google.android.contacts:id/floating_action_button", text="Create contact"),
                anchors=[{"resource_id": "com.google.android.contacts:id/toolbar_button"}],
            ),
            Edge("e2", "n2", "n2", "slot", tool="input_text", slot="slot1", target=TargetDescriptor(hint="First name")),
            Edge("e3", "n2", "n2", "slot", tool="input_text", slot="slot2", target=TargetDescriptor(hint="Last name")),
            Edge("e4", "n2", "n5", "nav", tool="click", typed="slot1,slot2", target=TargetDescriptor(resource_id="com.google.android.contacts:id/toolbar_button", text="Save")),
        ],
        start_nodes=["n1"],
        end_nodes=["n5"],
    )

    class Driver:
        def __init__(self):
            self.screen = R(list_pre)
            self.actions: list[tuple[str, str]] = []

        def current(self):
            return self.screen

        async def refresh(self):
            return self.screen

        async def act(self, tool, args, element):
            if tool == "click" and element and element.get("content-desc") == "Create contact":
                self.actions.append(("click", "Create contact"))
                self.screen = R(form_initial)
                return True, "ok"
            if tool == "input_text" and element and element.get("hint") == "First name":
                val = str(args.get("text") or "")
                self.actions.append(("input_text", f"First name={val}"))
                self.screen = R(form_after_first(val))
                return True, "ok"
            return False, "unexpected"

    drv = Driver()
    ap = Autopilot(split_wf, {"slot1": "Lan", "slot2": "Tran"}, verify_polls=1, poll_interval_s=0)
    assert ap.tau == DEFAULT_TAU
    assert ap._match(fp_list)[0] == "n1"
    rep = asyncio.run(ap.run(drv))
    assert ("click", "Create contact") in drv.actions
    assert ("input_text", "First name=Lan") in drv.actions
    assert any("Create contact" in s for s in rep.ran)
    assert any('input_text "Lan"' in s for s in rep.ran)
    # Unrelated screen does NOT pass _verified under default tau.
    assert not ap._verified(split_wf.edges[0], normalize(list_pre, 720, 1280), fp_list, "")


def test_l032_scrolls_above_keyboard_or_hides_it_to_fill_three_field_form():
    from artemis.workflow.autopilot import Autopilot
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.recorder import RawScreen
    from artemis.workflow.targets import TargetDescriptor, descriptor_for

    R = lambda els: RawScreen(els, 1000, 1000)  # noqa: E731
    pkg = "com.example.form"

    def kb_keys():
        return [
            {"package": "com.google.android.inputmethod.latin", "class": "android.view.View", "text": k, "bounds": f"[{i * 100},620][{i * 100 + 80},700]", "clickable": True}
            for i, k in enumerate("asdfgh", start=1)
        ]

    def build_tree(*, offset_y: int = 0, first: str = "", last: str = "", phone: str = "", kb: bool = False):
        bottom = 600 if kb else 980
        els = [
            {"package": pkg, "class": "android.widget.TextView", "text": "New entry", "bounds": "[20,20][300,80]", "clickable": False},
            {"package": pkg, "class": "android.widget.Button", "resource-id": f"{pkg}:id/save", "text": "Save", "bounds": "[760,20][960,90]", "clickable": True},
            {"package": pkg, "class": "android.widget.ScrollView", "resource-id": f"{pkg}:id/scroller", "bounds": f"[0,120][1000,{bottom}]", "scrollable": True},
        ]
        specs = [
            ("First name", first, 180 - offset_y, kb and not last),
            ("Last name", last, 660 - offset_y, kb and bool(first) and not phone),
            ("Phone", phone, 1020 - offset_y, kb and bool(last)),
        ]
        for hint, val, top, foc in specs:
            btm = top + 90
            if btm <= 120 or top >= bottom:
                continue
            els.append({
                "package": pkg,
                "class": "android.widget.EditText",
                "hint": hint,
                "text": val,
                "bounds": f"[80,{top}][920,{btm}]",
                "clickable": True,
                "editable": True,
                "focused": foc,
            })
        if kb:
            els.extend(kb_keys())
        return els

    detail = [
        {"package": pkg, "class": "android.widget.ImageButton", "resource-id": f"{pkg}:id/back", "content-desc": "Back", "bounds": "[0,20][100,100]", "clickable": True},
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/title", "text": "Contact details", "bounds": "[120,20][500,80]"},
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/summary", "text": "Saved contact", "bounds": "[100,200][900,320]"},
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/status", "text": "Ready", "bounds": "[100,340][900,420]"},
    ]

    fp_top = app_fingerprint(normalize(build_tree(), 1000, 1000))
    fp_done = app_fingerprint(normalize(detail, 1000, 1000))
    wf = Workflow(
        workflow_id=f"{pkg}__1",
        package=pkg,
        goal="fill 3-field form",
        slots={"slot1": "An", "slot2": "Le", "slot3": "0909998888"},
        nodes={"n1": Node("n1", [fp_top]), "n2": Node("n2", [fp_done])},
        edges=[
            Edge("e1", "n1", "n1", "slot", tool="input_text", slot="slot1", target=TargetDescriptor(hint="First name")),
            Edge("e2", "n1", "n1", "slot", tool="input_text", slot="slot2", target=TargetDescriptor(hint="Last name")),
            Edge("e3", "n1", "n1", "slot", tool="input_text", slot="slot3", target=TargetDescriptor(hint="Phone")),
            Edge(
                "e4",
                "n1",
                "n2",
                "nav",
                tool="click",
                typed="slot1,slot2,slot3",
                target=TargetDescriptor(resource_id=f"{pkg}:id/save", text="Save"),
                anchors=[{"resource_id": f"{pkg}:id/summary"}],
            ),
        ],
        start_nodes=["n1"],
        end_nodes=["n2"],
    )

    class Driver:
        def __init__(self):
            self.offset_y = 0
            self.first = ""
            self.last = ""
            self.phone = ""
            self.kb = False
            self.done = False
            self.log: list[tuple[str, Any]] = []

        def current(self):
            if self.done:
                return R(detail)
            return R(build_tree(offset_y=self.offset_y, first=self.first, last=self.last, phone=self.phone, kb=self.kb))

        async def refresh(self):
            return self.current()

        async def act(self, tool, args, element):
            self.log.append((tool, element.get("hint") if element else args))
            if tool == "press_key" and args.get("key") == "BACK":
                self.kb = False
                return True, "ok"
            if tool == "swipe":
                coords = args.get("coordinates") or []
                assert coords and coords[1] < 620, f"swipe must stay above keyboard: {coords}"
                self.offset_y += 280
                return True, "ok"
            if tool == "input_text" and element:
                hint = element.get("hint")
                val = str(args.get("text") or "")
                if hint == "First name":
                    self.first = val
                elif hint == "Last name":
                    self.last = val
                elif hint == "Phone":
                    self.phone = val
                self.kb = True
                return True, "ok"
            if tool == "click" and element and element.get("text") == "Save":
                self.done = True
                return True, "ok"
            return False, "unexpected"

    drv = Driver()
    ap = Autopilot(wf, {"slot1": "An", "slot2": "Le", "slot3": "0909998888"}, verify_polls=1, poll_interval_s=0)
    rep = asyncio.run(ap.run(drv))
    assert rep.finished, f"stopped early: {rep.stop_reason} / ran={rep.ran}"
    assert drv.first == "An" and drv.last == "Le" and drv.phone == "0909998888"
    assert any(t == "swipe" for t, _ in drv.log)
    assert any(t == "click" for t, _ in drv.log)


def test_l033_w0_matches_unordered_form_typing_and_flags_wrong_field():
    from scripts.workflow_replay_eval import _OfflineTraceDriver
    from artemis.workflow.recorder import RawScreen, RecordedStep

    def form(first="", last="", phone="", phone_y=700):
        return RawScreen(
            [
                {"package": "com.pkg", "class": "android.widget.EditText", "hint": "First name", "text": first, "bounds": "[80,200][920,280]", "clickable": True, "editable": True},
                {"package": "com.pkg", "class": "android.widget.EditText", "hint": "Last name", "text": last, "bounds": "[80,320][920,400]", "clickable": True, "editable": True},
                {"package": "com.pkg", "class": "android.widget.EditText", "hint": "Phone", "text": phone, "bounds": f"[80,{phone_y}][920,{phone_y + 80}]", "clickable": True, "editable": True},
                {"package": "com.pkg", "class": "android.widget.Button", "text": "Save", "bounds": "[760,20][960,90]", "clickable": True},
            ],
            1000,
            1000,
        )

    gt = [
        RecordedStep(
            tool="input_text",
            args={"text": "Lam", "target": 2},
            action={"normalized_coordinates": [300, 360]},
            pre=form(first="Nga"),
            post=form(first="Nga", last="Lam"),
        ),
        RecordedStep(tool="press_key", args={"key": "BACK"}, action={}, pre=form(first="Nga", last="Lam"), post=form(first="Nga", last="Lam", phone_y=600)),
        RecordedStep(
            tool="input_text",
            args={"text": "0904440004", "target": 3},
            action={"normalized_coordinates": [300, 640]},
            pre=form(first="Nga", last="Lam", phone_y=600),
            post=form(first="Nga", last="Lam", phone="0904440004", phone_y=600),
        ),
        RecordedStep(tool="click", args={"target": 4}, action={"normalized_coordinates": [850, 55]}, pre=form(first="Nga", last="Lam", phone="0904440004"), post=form()),
    ]
    drv = _OfflineTraceDriver(gt)
    phone_el = {"class": "android.widget.EditText", "hint": "Phone", "bounds": [80, 700, 920, 780]}
    last_el = {"class": "android.widget.EditText", "hint": "Last name", "bounds": [80, 320, 920, 400]}
    save_el = {"class": "android.widget.Button", "text": "Save", "bounds": [760, 20, 960, 90]}

    # Typing Phone before Last name on the same form matches cleanly with 0 mismatches.
    ok, _ = asyncio.run(drv.act("input_text", {"text": "0904440004", "target": 1}, phone_el))
    assert ok and drv.mismatches == 0 and drv.matched == 1
    ok, _ = asyncio.run(drv.act("input_text", {"text": "Lam", "target": 1}, last_el))
    assert ok and drv.mismatches == 0 and drv.matched == 2
    ok, _ = asyncio.run(drv.act("click", {"target": 1}, save_el))
    assert ok and drv.mismatches == 0 and drv.matched == 3

    # Typing Phone value into Last name field MUST still count as a mismatch.
    bad_drv = _OfflineTraceDriver(gt)
    asyncio.run(bad_drv.act("input_text", {"text": "0904440004", "target": 1}, last_el))
    assert bad_drv.mismatches == 1 and bad_drv.matched == 0


def test_l034_create_contact_workflow_does_not_overwrite_prefilled_edit_contact(tmp_path):
    from artemis.workflow.autopilot import Autopilot
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.recorder import RawScreen, RecordedStep, record_run
    from artemis.workflow.store import WorkflowStore

    R = lambda els: RawScreen(els, 1000, 1000)  # noqa: E731
    pkg = "com.example.contacts"

    def contact_form(*, title="Create contact", first="", last="", phone=""):
        return [
            {"package": pkg, "class": "android.widget.TextView", "text": title, "bounds": "[20,20][400,80]"},
            {"package": pkg, "class": "android.widget.Button", "resource-id": f"{pkg}:id/save", "text": "Save", "bounds": "[760,20][960,90]", "clickable": True},
            {"package": pkg, "class": "android.widget.EditText", "hint": "First name", "text": first, "bounds": "[80,200][920,280]", "clickable": True, "editable": True},
            {"package": pkg, "class": "android.widget.EditText", "hint": "Last name", "text": last, "bounds": "[80,320][920,400]", "clickable": True, "editable": True},
            {"package": pkg, "class": "android.widget.EditText", "hint": "Phone", "text": phone, "bounds": "[80,440][920,520]", "clickable": True, "editable": True},
        ]

    detail = [
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/title", "text": "An Le", "bounds": "[20,20][400,80]"},
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/phone", "text": "0901112222", "bounds": "[80,200][500,260]"},
        {"package": pkg, "class": "android.widget.ImageButton", "resource-id": f"{pkg}:id/edit", "content-desc": "Edit", "bounds": "[800,20][900,100]", "clickable": True},
    ]

    # Record a Create contact workflow starting from empty fields.
    rec_steps = [
        RecordedStep(tool="input_text", args={"text": "An", "target": 3}, action={"normalized_coordinates": [300, 240]}, pre=R(contact_form()), post=R(contact_form(first="An"))),
        RecordedStep(tool="input_text", args={"text": "Le", "target": 4}, action={"normalized_coordinates": [300, 360]}, pre=R(contact_form(first="An")), post=R(contact_form(first="An", last="Le"))),
        RecordedStep(tool="input_text", args={"text": "0901112222", "target": 5}, action={"normalized_coordinates": [300, 480]}, pre=R(contact_form(first="An", last="Le")), post=R(contact_form(first="An", last="Le", phone="0901112222"))),
        RecordedStep(tool="click", args={"target": 2}, action={"normalized_coordinates": [850, 55], "target_text": "Save", "target_resource_id": f"{pkg}:id/save"}, pre=R(contact_form(first="An", last="Le", phone="0901112222")), post=R(detail)),
    ]
    wf = record_run(rec_steps, goal="create contact An Le phone 0901112222", slot_values={"slot1": "An", "slot2": "Le", "slot3": "0901112222"})
    assert wf is not None
    assert all(e.empty_on_record for e in wf.edges if e.tool == "input_text")

    store = WorkflowStore(tmp_path)
    store.save(wf)
    reloaded = store.load(pkg, wf.workflow_id)
    assert reloaded is not None and all(e.empty_on_record for e in reloaded.edges if e.tool == "input_text")

    # Ensure Edit Contact screen matches form node cleanly at tau=0.80 so ONLY the L-034 guard stops it.
    edit_pre = contact_form(title="Edit contact", first="Huy", last="", phone="")
    reloaded.nodes[wf.start_nodes[0]].add_fp(app_fingerprint(normalize(edit_pre, 1000, 1000)))

    class SpyDriver:
        def __init__(self, scr):
            self.scr = scr
            self.actions: list[tuple[str, Any]] = []

        def current(self):
            return self.scr

        async def refresh(self):
            return self.scr

        async def act(self, tool, args, element):
            self.actions.append((tool, args))
            return True, "ok"

    drv = SpyDriver(R(edit_pre))
    ap = Autopilot(reloaded, {"slot1": "Minh", "slot2": "Pham", "slot3": "0909888777"}, verify_polls=1, poll_interval_s=0)
    rep = asyncio.run(ap.run(drv))
    assert drv.actions == []
    assert rep.acted == 0
    assert "existing data" in rep.stop_reason

    # Placeholder hint inside `text` (text == hint) still counts as empty and runs normally.
    hint_pre = contact_form(first="First name", last="Last name", phone="Phone")
    drv2 = SpyDriver(R(hint_pre))
    ap2 = Autopilot(reloaded, {"slot1": "Minh", "slot2": "Pham", "slot3": "0909888777"}, verify_polls=1, poll_interval_s=0)
    asyncio.run(ap2.run(drv2))
    assert len(drv2.actions) >= 1 and drv2.actions[0][0] == "input_text"


def test_l035_autopilot_commands_pass_real_executor_translation_contract():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from artemis.mcp.action_executor import McpActionExecutor
    from artemis.workflow.autopilot import Autopilot
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.recorder import RawScreen
    from artemis.workflow.targets import TargetDescriptor, descriptor_for

    R = lambda els: RawScreen(els, 1000, 1000)  # noqa: E731
    pkg = "com.example.contract"

    screen_top = [
        {"package": pkg, "class": "android.widget.Button", "resource-id": f"{pkg}:id/open", "text": "Open", "bounds": "[50,50][250,130]", "clickable": True},
        {"package": pkg, "class": "android.widget.ScrollView", "resource-id": f"{pkg}:id/scroll", "bounds": "[0,150][1000,950]", "scrollable": True},
        {"package": pkg, "class": "android.widget.EditText", "hint": "First", "text": "", "bounds": "[80,200][920,280]", "clickable": True, "editable": True, "focused": False},
        {"package": pkg, "class": "android.widget.EditText", "hint": "Second", "text": "", "bounds": "[80,320][920,400]", "clickable": True, "editable": True, "focused": False},
    ]
    screen_scrolled = [
        {"package": pkg, "class": "android.widget.ScrollView", "resource-id": f"{pkg}:id/scroll", "bounds": "[0,150][1000,950]", "scrollable": True},
        {"package": pkg, "class": "android.widget.EditText", "hint": "First", "text": "A", "bounds": "[80,160][920,240]", "clickable": True, "editable": True, "focused": False},
        {"package": pkg, "class": "android.widget.EditText", "hint": "Second", "text": "", "bounds": "[80,280][920,360]", "clickable": True, "editable": True, "focused": False},
        {"package": pkg, "class": "android.widget.EditText", "hint": "Third", "text": "", "bounds": "[80,400][920,480]", "clickable": True, "editable": True, "focused": False},
        {"package": pkg, "class": "android.widget.Button", "resource-id": f"{pkg}:id/done", "text": "Done", "bounds": "[760,40][960,120]", "clickable": True},
    ]

    screen_done = [
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/done_title", "text": "Finished", "bounds": "[20,20][400,80]"},
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/done_sub", "text": "Complete", "bounds": "[20,120][400,180]"},
        {"package": pkg, "class": "android.widget.Button", "resource-id": f"{pkg}:id/ok_btn", "text": "OK", "bounds": "[400,400][600,480]", "clickable": True},
    ]

    fp1 = app_fingerprint(normalize(screen_top, 1000, 1000))
    fp2 = app_fingerprint(normalize(screen_scrolled, 1000, 1000))
    fp_done = app_fingerprint(normalize(screen_done, 1000, 1000))
    wf = Workflow(
        workflow_id=f"{pkg}__1",
        package=pkg,
        goal="fill form",
        slots={"s1": "A", "s2": "B", "s3": "C"},
        nodes={"n1": Node("n1", [fp1, fp2]), "n2": Node("n2", [fp_done])},
        edges=[
            Edge("e1", "n1", "n1", "slot", tool="input_text", slot="s1", target=TargetDescriptor(hint="First")),
            Edge("e2", "n1", "n1", "slot", tool="input_text", slot="s3", target=TargetDescriptor(hint="Third")),
            Edge("e3", "n1", "n2", "nav", tool="press_key", key="ENTER", typed="s1,s3"),
            Edge("e4", "n2", "n2", "nav", tool="click", target=TargetDescriptor(resource_id=f"{pkg}:id/open", text="Open")),
        ],
        start_nodes=["n1"],
        end_nodes=["n2"],
    )

    ex = McpActionExecutor.__new__(McpActionExecutor)
    ex.ctx = SimpleNamespace(device=SimpleNamespace(device_width=1000, device_height=1000), last_raw_observation=None)
    ex.actuator = MagicMock()
    ex.agent_name = "flash"

    # Every command emitted by FlashWorkflowHook (manage_app) and Autopilot must pass _translate.
    hook_cmd = ("manage_app", {"action": "launch", "app_name": pkg})
    wire_name, wire_args, _, _ = ex._translate(hook_cmd[0], hook_cmd[1], None)
    assert wire_name == "manage_app" and wire_args["app_name"] == pkg

    translated_tools: list[str] = []

    class ContractDriver:
        def __init__(self):
            self.scrolled = False
            self.kb = False
            self.s1 = False
            self.s3 = False

        def current(self):
            if getattr(self, "done_enter", False):
                return R(screen_done)
            return R(screen_scrolled if self.scrolled else screen_top)

        async def refresh(self):
            return self.current()

        async def act(self, tool, args, element):
            idx_els = None
            if element is not None:
                l, t, r, b = element["bounds"]
                idx_els = [{
                    "center": [(l + r) // 2, (t + b) // 2],
                    "bounds": [l, t, r, b],
                    "text": element.get("text") or element.get("content-desc") or "",
                    "resource_id": element.get("resource-id") or "",
                    "class": element.get("class") or "",
                }]
            state = SimpleNamespace(indexed_elements=idx_els or [], latest_ui_hierarchy=None)
            wname, wargs, _, _ = ex._translate(tool, args, state, index_elements=idx_els)
            assert wname
            translated_tools.append(tool)
            if tool == "swipe":
                assert args.get("target_description")
                assert "start" in wargs and "end" in wargs
                self.scrolled = True
                return True, "ok"
            if tool == "input_text":
                for el in screen_scrolled:
                    if el.get("hint") == "Third" and element and element.get("hint") == "Third":
                        el["text"] = args["text"]
                        self.s3 = True
                if element and element.get("hint") == "First":
                    screen_top[2]["text"] = args["text"]
                    screen_scrolled[1]["text"] = args["text"]
                    self.s1 = True
                return True, "ok"
            if tool == "press_key":
                self.kb = True
                self.done_enter = True
                return True, "ok"
            return True, "ok"

    drv = ContractDriver()
    # Trigger click, input_text, swipe, press_key through real _translate:
    ap = Autopilot(wf, {"s1": "A", "s3": "C"}, verify_polls=1, poll_interval_s=0)
    asyncio.run(ap.run(drv))
    assert "swipe" in translated_tools and "input_text" in translated_tools and "press_key" in translated_tools
    # Also verify click + focused input_text + press_key BACK directly through _translate:
    st_click = SimpleNamespace(indexed_elements=[{"center": [150, 90], "bounds": [50, 50, 250, 130], "text": "Open", "resource_id": f"{pkg}:id/open", "class": "android.widget.Button"}])
    assert ex._translate("click", {"target": 1}, st_click, index_elements=st_click.indexed_elements)[0] == "click"
    assert ex._translate("input_text", {"text": "abc"}, st_click)[0] == "input_text"
    assert ex._translate("press_key", {"key": "BACK"}, st_click)[0] == "press_key"


def test_l036_save_refuses_when_phone_value_sits_in_last_name_field():
    from artemis.workflow.autopilot import Autopilot
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.recorder import RawScreen
    from artemis.workflow.targets import TargetDescriptor, descriptor_for

    R = lambda els: RawScreen(els, 1000, 1000)  # noqa: E731
    pkg = "com.example.contacts"

    # Simulates ada566df ("Trang Mai"): Last name contains "Mai\n0907770007", Phone is empty.
    def corrupted_form(*, include_phone: bool = True, phone_text: str = ""):
        els = [
            {"package": pkg, "class": "android.widget.TextView", "text": "Create contact", "bounds": "[20,20][400,80]"},
            {"package": pkg, "class": "android.widget.Button", "resource-id": f"{pkg}:id/save", "text": "Save", "bounds": "[760,20][960,90]", "clickable": True},
            {"package": pkg, "class": "android.widget.EditText", "hint": "First name", "text": "Trang", "bounds": "[80,200][920,280]", "clickable": True, "editable": True},
            {"package": pkg, "class": "android.widget.EditText", "hint": "Last name", "text": "Mai\n0907770007", "bounds": "[80,320][920,400]", "clickable": True, "editable": True},
        ]
        if include_phone:
            els.append({"package": pkg, "class": "android.widget.EditText", "hint": "Phone", "text": phone_text, "bounds": "[80,440][920,520]", "clickable": True, "editable": True})
        return els

    detail = [
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/title", "text": "Trang Mai", "bounds": "[20,20][400,80]"},
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/phone", "text": "0907770007", "bounds": "[80,200][500,260]"},
    ]
    fp_form = app_fingerprint(normalize(corrupted_form(), 1000, 1000))
    fp_done = app_fingerprint(normalize(detail, 1000, 1000))
    wf = Workflow(
        workflow_id=f"{pkg}__1",
        package=pkg,
        goal="create contact",
        slots={"slot1": "Trang", "slot2": "Mai", "slot3": "0907770007"},
        nodes={"n1": Node("n1", [fp_form]), "n2": Node("n2", [fp_done])},
        edges=[
            Edge("e1", "n1", "n1", "slot", tool="input_text", slot="slot1", target=TargetDescriptor(hint="First name")),
            Edge("e2", "n1", "n1", "slot", tool="input_text", slot="slot2", target=TargetDescriptor(hint="Last name")),
            Edge("e3", "n1", "n1", "slot", tool="input_text", slot="slot3", target=TargetDescriptor(hint="Phone")),
            Edge("e4", "n1", "n2", "nav", tool="click", typed="slot1,slot2,slot3", target=TargetDescriptor(resource_id=f"{pkg}:id/save", text="Save"), anchors=[{"resource_id": f"{pkg}:id/title"}]),
        ],
        start_nodes=["n1"],
        end_nodes=["n2"],
    )
    slots = {"slot1": "Trang", "slot2": "Mai", "slot3": "0907770007"}

    # 1) Under L-045 exact matching, Last name holding "Mai\n0907770007" is NOT
    #    counted as clean slot2 held: Autopilot blocks Save and re-types Last name;
    #    and when Last name is clean while "0907770007" sits in a third field (Notes)
    #    and Phone is hidden, Save stays blocked (edge is None).
    norm_hidden = normalize(corrupted_form(include_phone=False), 1000, 1000)
    ap1 = Autopilot(wf, slots, verify_polls=1, poll_interval_s=0)
    edge, el, _, why = ap1._pick("n1", norm_hidden)
    assert edge is None or edge.id != "e4"  # Save is NEVER allowed
    assert why != "several recorded steps apply here"

    clean_ln_notes_corrupted = [
        {"package": pkg, "class": "android.widget.TextView", "text": "Create contact", "bounds": "[20,20][400,80]"},
        {"package": pkg, "class": "android.widget.Button", "resource-id": f"{pkg}:id/save", "text": "Save", "bounds": "[760,20][960,90]", "clickable": True},
        {"package": pkg, "class": "android.widget.EditText", "hint": "First name", "text": "Trang", "bounds": "[80,200][920,280]", "clickable": True, "editable": True},
        {"package": pkg, "class": "android.widget.EditText", "hint": "Last name", "text": "Mai", "bounds": "[80,320][920,400]", "clickable": True, "editable": True},
        {"package": pkg, "class": "android.widget.EditText", "hint": "Notes", "text": "0907770007", "bounds": "[80,440][920,520]", "clickable": True, "editable": True},
    ]
    edge_notes, el_notes, _, why_notes = ap1._pick("n1", normalize(clean_ln_notes_corrupted, 1000, 1000))
    assert edge_notes is None and el_notes is None
    assert why_notes != "several recorded steps apply here"

    # 2) When Phone IS shown empty while Last name holds "Mai" and Notes holds "0907770007":
    #    Save stays blocked and Autopilot fills Phone instead of clicking Save!
    clean_ln_phone_empty = clean_ln_notes_corrupted + [
        {"package": pkg, "class": "android.widget.EditText", "hint": "Phone", "text": "", "bounds": "[80,560][920,640]", "clickable": True, "editable": True},
    ]
    norm_phone_empty = normalize(clean_ln_phone_empty, 1000, 1000)
    ap2 = Autopilot(wf, slots, verify_polls=1, poll_interval_s=0)
    edge2, el2, val2, _ = ap2._pick("n1", norm_phone_empty)
    assert edge2 is not None and edge2.id == "e3" and el2 and el2.get("hint") == "Phone" and val2 == "0907770007"


def test_l037_executor_refuses_unrelated_overwrite_on_targeted_field_but_allows_typo_fix():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from artemis.mcp.action_executor import McpActionExecutor

    elements = [
        {"package": "com.google.android.contacts", "class": "android.widget.EditText", "hint": "Last name", "text": "Mai", "bounds": "[104,565][608,682]", "clickable": True, "editable": True, "focused": True},
    ]
    ex = McpActionExecutor.__new__(McpActionExecutor)
    ex.agent_name = "flash"
    ex.ctx = SimpleNamespace(
        device=SimpleNamespace(device_width=720, device_height=1280),
        last_raw_observation={"elements": elements, "width": 720, "height": 1280},
        data_engine=None,
    )
    ex.actuator = MagicMock()
    ex.actuator.extensions.return_value = []
    ex._typed_values = ["trang", "mai"]
    ex._last_point_action = None
    ex._last_elements_signature = None
    ex._last_image_hash = None
    ex._ineffective_taps = []
    ex._session = AsyncMock()
    ex._session.started = True

    state = SimpleNamespace(
        indexed_elements=[{
            "center": [356, 623],
            "bounds": [104, 565, 608, 682],
            "text": "Mai",
            "hint": "Last name",
            "resource_id": "",
            "class": "android.widget.EditText",
        }]
    )

    # Unrelated phone number targeted directly at Last name field holding "Mai" -> refused!
    res_bad = asyncio.run(ex.execute("input_text", {"target": 1, "text": "\n0907770007"}, "call_1", state))
    assert not res_bad.status == "success"
    assert "Mai" in res_bad.text_summary and "refused" in res_bad.text_summary.lower()

    # Editing / extending or typo-fixing the same field value IS allowed:
    assert ex._input_overwrites_typed_field([494, 487], "Mai Nguyen") is None
    assert ex._input_overwrites_typed_field([494, 487], "Maii") is None

    # Pre-existing text NOT typed earlier in this run IS allowed:
    ex._typed_values = ["other"]
    assert ex._input_overwrites_typed_field([494, 487], "0907770007") is None


def test_l039_autopilot_verifies_end_screen_by_scrolling_and_executor_blocks_entry_once():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from artemis.mcp.action_types import ActionResult
    from artemis.mcp.action_executor import McpActionExecutor
    from artemis.workflow.autopilot import Autopilot
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.recorder import RawScreen
    from artemis.workflow.targets import TargetDescriptor, descriptor_for

    R = lambda els: RawScreen(els, 1000, 1000)  # noqa: E731
    pkg = "com.example.clock"

    def clock_list(offset: int = 0, include_budapest: bool = True):
        cities = ["Hanoi", "Tokyo", "Seoul", "London", "Paris", "Berlin"]
        if include_budapest:
            cities.append("Budapest")
        els = [
            {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/title", "text": "Clock", "bounds": "[20,20][300,80]"},
            {"package": pkg, "class": "android.widget.ImageButton", "resource-id": f"{pkg}:id/fab", "content-desc": "Add city", "bounds": "[420,820][580,920]", "clickable": True},
            {"package": pkg, "class": "androidx.recyclerview.widget.RecyclerView", "resource-id": f"{pkg}:id/cities", "bounds": "[0,100][1000,800]", "scrollable": True},
        ]
        # Each scroll shifts items up by 2 rows; on offset>=2, Budapest scrolls into view.
        for i, city in enumerate(cities[offset:]):
            top = 120 + i * 260
            if top >= 780:
                continue
            els.append({
                "package": pkg,
                "class": "android.widget.TextView",
                "resource-id": f"{pkg}:id/city_name",
                "text": city,
                "bounds": f"[40,{top}][500,{top + 100}]",
            })
        return els

    search_scr = [
        {"package": pkg, "class": "android.widget.EditText", "resource-id": f"{pkg}:id/search", "hint": "Search for a city", "bounds": "[100,60][900,140]", "clickable": True, "editable": True},
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/city_name", "text": "Budapest, Hungary", "bounds": "[80,200][800,280]", "clickable": True},
    ]

    fp_clock = app_fingerprint(normalize(clock_list(0), 1000, 1000))
    fp_search = app_fingerprint(normalize(search_scr, 1000, 1000))
    wf = Workflow(
        workflow_id=f"{pkg}__1",
        package=pkg,
        goal="add city Budapest",
        slots={"slot1": "Budapest"},
        nodes={"n1": Node("n1", [fp_clock]), "n2": Node("n2", [fp_search])},
        edges=[
            Edge("e1", "n1", "n2", "nav", tool="click", target=TargetDescriptor(resource_id=f"{pkg}:id/fab", text="Add city")),
            Edge("e2", "n2", "n2", "slot", tool="input_text", slot="slot1", target=TargetDescriptor(resource_id=f"{pkg}:id/search")),
            Edge("e3", "n2", "n1", "slot", tool="click", slot="slot1", target=TargetDescriptor(slot="slot1")),
        ],
        start_nodes=["n1"],
        end_nodes=["n1"],
    )

    class Driver:
        def __init__(self, include_budapest=True):
            self.offset = 0
            self.include_budapest = include_budapest

        def current(self):
            return R(clock_list(self.offset, include_budapest=self.include_budapest))

        async def refresh(self):
            return self.current()

        async def act(self, tool, args, element):
            if tool == "swipe":
                assert args.get("target_description")
                if self.offset < 4:
                    self.offset += 2
                return True, "ok"
            return True, "ok"

    ap = Autopilot(wf, {"slot1": "Budapest"}, verify_polls=1, poll_interval_s=0)
    ap._done = {"e1", "e2", "e3"}  # already completed flow back on end node n1
    drv = Driver(include_budapest=True)
    rep = asyncio.run(ap.run(drv))
    assert rep.finished
    assert rep.verified_found == ["Budapest"] and rep.verified_missing == []
    assert rep.verify_scrolls >= 1
    msg = rep.notice(wf.workflow_id, scrollable=True)
    assert "CONFIRMED" in msg and "Budapest" in msg and "most likely done" not in msg

    # When missing after scrolling to end of list, report missing truthfully:
    ap_miss = Autopilot(wf, {"slot1": "Budapest"}, verify_polls=1, poll_interval_s=0)
    ap_miss._done = {"e1", "e2", "e3"}
    rep_miss = asyncio.run(ap_miss.run(Driver(include_budapest=False)))
    assert rep_miss.verified_missing == ["Budapest"] and rep_miss.verified_found == []
    assert "did NOT see 'Budapest'" in rep_miss.notice(wf.workflow_id, scrollable=True)

    # One-shot executor guard: first tap on Add city FAB is refused with check summary; retry passes.
    ex = McpActionExecutor.__new__(McpActionExecutor)
    ex.agent_name = "flash"
    ex.ctx = SimpleNamespace(
        device=SimpleNamespace(device_width=1000, device_height=1000),
        last_raw_observation={"elements": clock_list(4, True), "width": 1000, "height": 1000},
        data_engine=None,
    )
    ex.actuator = MagicMock()
    ex.actuator.extensions.return_value = []
    ex._typed_values = []
    ex._last_point_action = None
    ex._last_elements_signature = None
    ex._last_image_hash = None
    ex._ineffective_taps = []
    ex._workflow_end_guard = None
    ex._session = AsyncMock()
    ex._session.started = True
    ex._session.call = AsyncMock(return_value=ActionResult.success("click", "Clicked"))
    ex._session.observe = AsyncMock(side_effect=RuntimeError("skip observe"))

    ex.arm_workflow_end_guard(
        [{"tool": "click", "resource_id": f"{pkg}:id/fab", "text": "Add city", "bounds": [420, 820, 580, 920]}],
        rep.end_check_summary(),
    )
    state = SimpleNamespace(
        indexed_elements=[{
            "center": [500, 870],
            "bounds": [420, 820, 580, 920],
            "text": "Add city",
            "resource_id": f"{pkg}:id/fab",
            "class": "android.widget.ImageButton",
        }]
    )
    res1 = asyncio.run(ex.execute("click", {"target": 1}, "c1", state, skip_observe=True))
    assert res1.status != "success"
    assert "Budapest" in res1.text_summary and "refused once" in res1.text_summary.lower()
    # Second tap (genuine retry) clears the one-shot block and succeeds:
    res2 = asyncio.run(ex.execute("click", {"target": 1}, "c2", state, skip_observe=True))
    assert res2.status == "success"


def test_l040_compact_list_shows_selected_on_item_and_enclosing_row():
    from artemis.utils.visualization import format_minimal_list_with_elements

    pkg = "com.google.android.deskclock"
    nodes = [
        # Full-screen wrapper with selected=false must never leak.
        {"package": pkg, "class": "android.widget.FrameLayout", "text": "", "bounds": "[0,0][1000,1000]", "selected": "false"},
        # 1) Both row and child have selected="true" (exact 1f14e556 step 11 pattern).
        {"package": pkg, "class": "android.widget.LinearLayout", "text": "", "bounds": "[16,150][980,260]", "clickable": "true", "selected": "true"},
        {"package": pkg, "class": "android.widget.TextView", "text": "Budapest, Hungary", "bounds": "[100,180][600,230]", "selected": "true"},
        # 2) Only enclosing row container carries selected="true" while TextView says "false".
        {"package": pkg, "class": "android.widget.LinearLayout", "text": "", "bounds": "[16,280][980,390]", "clickable": "true", "selected": "true"},
        {"package": pkg, "class": "android.widget.TextView", "text": "Warsaw, Poland", "bounds": "[100,310][600,360]", "selected": "false"},
        # 3) Unselected row stays unmarked.
        {"package": pkg, "class": "android.widget.LinearLayout", "text": "", "bounds": "[16,410][980,520]", "clickable": "true", "selected": "false"},
        {"package": pkg, "class": "android.widget.TextView", "text": "Vienna, Austria", "bounds": "[100,440][600,490]", "selected": "false"},
    ]
    compact_text, els, _ = format_minimal_list_with_elements(nodes, 1000, 1000, compact=True)
    lines = compact_text.splitlines()
    assert lines[0] == '[1] "Budapest, Hungary" 100,180,600,230 (selected)'
    assert lines[1] == '[2] "Warsaw, Poland" 100,310,600,360 (selected)'
    assert lines[2] == '[3] "Vienna, Austria" 100,440,600,490'
    assert str(els[0].get("selected")).lower() == "true"
    assert els[1].get("selected") is True
    assert str(els[2].get("selected")).lower() == "false"


def test_l041_autopilot_scrolls_beyond_three_until_end_or_found():
    from pathlib import Path

    from artemis.workflow.autopilot import MAX_SCROLL_SEARCH, Autopilot
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.recorder import RawScreen
    from artemis.workflow.targets import TargetDescriptor, descriptor_for

    assert MAX_SCROLL_SEARCH >= 10
    prompt_path = Path(__file__).resolve().parents[3] / "artemis" / "agents" / "flash" / "flash_runner.md"
    prompt_text = prompt_path.read_text(encoding="utf-8")
    assert "at most 3 swipes" not in prompt_text

    R = lambda els: RawScreen(els, 1000, 1000)  # noqa: E731
    pkg = "com.example.longlist"

    def make_page(idx: int):
        label = "Target Item" if idx == 5 else f"Row {idx}"
        return [
            {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/header", "text": "Items", "bounds": "[20,20][300,80]"},
            {"package": pkg, "class": "androidx.recyclerview.widget.RecyclerView", "resource-id": f"{pkg}:id/list", "bounds": "[0,100][1000,900]", "scrollable": True},
            {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/item", "text": label, "bounds": "[60,200][800,300]", "clickable": True},
            {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/sub", "text": f"Detail {idx}", "bounds": "[60,320][800,380]"},
        ]

    done_scr = [
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/done", "text": "Opened Target", "bounds": "[60,200][800,300]"},
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/done2", "text": "Ready", "bounds": "[60,320][800,380]"},
        {"package": pkg, "class": "android.widget.Button", "resource-id": f"{pkg}:id/ok", "text": "OK", "bounds": "[60,500][300,580]"},
    ]
    fp_list = app_fingerprint(normalize(make_page(0), 1000, 1000))
    fp_done = app_fingerprint(normalize(done_scr, 1000, 1000))
    wf = Workflow(
        workflow_id=f"{pkg}__1",
        package=pkg,
        goal="open target item",
        nodes={"n1": Node("n1", [fp_list]), "n2": Node("n2", [fp_done])},
        edges=[Edge("e1", "n1", "n2", "nav", tool="click", target=TargetDescriptor(text="Target Item"), anchors=[{"resource_id": f"{pkg}:id/done"}])],
        start_nodes=["n1"],
        end_nodes=["n2"],
    )

    class LongDriver:
        def __init__(self):
            self.page = 0
            self.done = False
            self.swipes = 0

        def current(self):
            return R(done_scr if self.done else make_page(self.page))

        async def refresh(self):
            return self.current()

        async def act(self, tool, args, element):
            if tool == "swipe":
                self.swipes += 1
                self.page += 1
                return True, "ok"
            if tool == "click" and element and element.get("text") == "Target Item":
                self.done = True
                return True, "ok"
            return False, "unexpected"

    drv = LongDriver()
    rep = asyncio.run(Autopilot(wf, {}, verify_polls=1, poll_interval_s=0).run(drv))
    assert rep.finished and drv.done and drv.swipes == 5


def test_l042_verifies_on_revisit_when_slots_done_even_if_end_node_is_search_screen():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from artemis.agents.flash.workflow_hook import FlashWorkflowHook
    from artemis.workflow.autopilot import Autopilot
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.recorder import RawScreen
    from artemis.workflow.targets import TargetDescriptor, descriptor_for

    R = lambda els: RawScreen(els, 1000, 1000)  # noqa: E731
    pkg = "com.google.android.deskclock"

    def clock_list(offset: int = 0):
        cities = ["Accra", "Tokyo", "London", "Paris", "Kyiv"]
        els = [
            {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/title", "text": "Clock", "bounds": "[20,20][300,80]"},
            {"package": pkg, "class": "android.widget.ImageButton", "resource-id": f"{pkg}:id/fab", "content-desc": "Add city", "bounds": "[420,820][580,920]", "clickable": True},
            {"package": pkg, "class": "androidx.recyclerview.widget.RecyclerView", "resource-id": f"{pkg}:id/cities", "bounds": "[0,100][1000,800]", "scrollable": True},
        ]
        for i, city in enumerate(cities[offset:]):
            top = 120 + i * 260
            if top >= 780:
                continue
            els.append({"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/city_name", "text": city, "bounds": f"[40,{top}][500,{top + 100}]"})
        return els

    search_empty = [
        {"package": pkg, "class": "android.widget.ImageButton", "resource-id": f"{pkg}:id/back", "content-desc": "Navigate up", "bounds": "[20,60][90,140]", "clickable": True},
        {"package": pkg, "class": "android.widget.EditText", "resource-id": f"{pkg}:id/search", "hint": "Search for a city", "bounds": "[100,60][900,140]", "clickable": True, "editable": True},
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/city_hint", "text": "Recent cities", "bounds": "[80,200][800,260]"},
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/city_sub", "text": "Empty", "bounds": "[80,280][800,340]"},
    ]
    search_results = [
        {"package": pkg, "class": "android.widget.ImageButton", "resource-id": f"{pkg}:id/back", "content-desc": "Navigate up", "bounds": "[20,60][90,140]", "clickable": True},
        {"package": pkg, "class": "android.widget.EditText", "resource-id": f"{pkg}:id/search", "text": "Kyiv", "hint": "Search for a city", "bounds": "[100,60][900,140]", "clickable": True, "editable": True},
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/city_name", "text": "Kyiv, Ukraine", "bounds": "[80,200][800,280]", "clickable": True},
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/city_time", "text": "2:00 PM", "bounds": "[820,200][960,280]"},
    ]
    fp_clock = app_fingerprint(normalize(clock_list(0), 1000, 1000))
    fp_search = app_fingerprint(normalize(search_empty, 1000, 1000))
    fp_results = app_fingerprint(normalize(search_results, 1000, 1000))
    fp_accra_end = app_fingerprint(normalize([
        {"package": pkg, "class": "android.widget.EditText", "resource-id": f"{pkg}:id/search", "text": "Accra", "bounds": "[100,60][900,140]", "clickable": True, "editable": True},
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/city_name", "text": "Accra, Ghana", "bounds": "[80,200][800,280]", "selected": "true"},
        {"package": pkg, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/extra", "text": "Added", "bounds": "[80,320][800,380]"},
    ], 1000, 1000))
    # Mimics Accra recording ending on search/added state (n3), NOT the Clock list (n1):
    wf = Workflow(
        workflow_id=f"{pkg}__1",
        package=pkg,
        goal="add city Kyiv",
        slots={"slot1": "Kyiv"},
        nodes={"n1": Node("n1", [fp_clock]), "n2": Node("n2", [fp_search, fp_results]), "n3": Node("n3", [fp_accra_end])},
        edges=[
            Edge("e1", "n1", "n2", "nav", tool="click", target=TargetDescriptor(resource_id=f"{pkg}:id/fab", text="Add city")),
            Edge("e2", "n2", "n2", "slot", tool="input_text", slot="slot1", target=TargetDescriptor(resource_id=f"{pkg}:id/search")),
            Edge("e3", "n2", "n1", "slot", tool="click", slot="slot1", target=TargetDescriptor(slot="slot1")),
        ],
        start_nodes=["n1"],
        end_nodes=["n3"],  # search/confirmation screen, NOT Clock list n1!
    )

    class Driver:
        def __init__(self):
            self.offset = 0
            self.stage = "clock_top"

        def current(self):
            if self.stage == "search":
                return R(search_empty)
            if self.stage == "results":
                return R(search_results)
            return R(clock_list(self.offset))

        async def refresh(self):
            return self.current()

        async def act(self, tool, args, element):
            if tool == "click" and element and element.get("content-desc") == "Add city":
                self.stage = "search"
                return True, "ok"
            if tool == "input_text":
                self.stage = "results"
                return True, "ok"
            if tool == "click" and element and "Kyiv" in (element.get("text") or ""):
                self.stage = "clock_back"
                return True, "ok"
            if tool == "swipe":
                self.offset += 2
                return True, "ok"
            return False, "unexpected"

    drv = Driver()
    ap = Autopilot(wf, {"slot1": "Kyiv"}, verify_polls=1, poll_interval_s=0)
    rep = asyncio.run(ap.run(drv))
    assert rep.finished
    assert rep.verified_found == ["Kyiv"] and rep.verified_missing == []
    assert rep.verify_scrolls >= 1
    assert "CONFIRMED" in rep.notice(wf.workflow_id, scrollable=True)

    # Hook also arms the one-shot executor guard when finished via L-042 revisit:
    ex = MagicMock()
    hook = FlashWorkflowHook(SimpleNamespace(ctx=SimpleNamespace(), executor=ex, goal="add city Kyiv"), "replay")
    hook.workflow = wf
    hook.slot_values = {"slot1": "Kyiv"}
    hook._revisit_warned = set()
    hook._arm_end_guard(rep)
    ex.arm_workflow_end_guard.assert_called_once()


def test_l044_unsaved_form_with_typed_fields_does_not_confirm_completion():
    """L-044: values still sitting inside editable EditText fields must not trigger
    L-039 confirmation or L-042 end-of-workflow completion on an unsaved form."""
    import asyncio
    from unittest.mock import MagicMock

    from artemis.agents.flash.workflow_hook import FlashWorkflowHook
    from artemis.workflow.autopilot import Autopilot
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.recorder import RawScreen
    from artemis.workflow.store import Edge, Node, Workflow
    from artemis.workflow.targets import TargetDescriptor, descriptor_for

    pkg = "com.example.contacts"
    n = lambda b, text="", hint="", cls="android.widget.TextView", rid="", desc="": {
        "bounds": b,
        "text": text,
        "hint": hint,
        "content-desc": desc,
        "class": cls,
        "resource-id": rid,
        "package": pkg,
        "visible": True,
        "clickable": True,
    }
    home_empty = [
        n([40, 60, 400, 120], text="Contacts", rid=f"{pkg}:id/title"),
        n([820, 820, 940, 940], desc="Create contact", rid=f"{pkg}:id/fab"),
    ]
    home_saved = [
        n([40, 60, 400, 120], text="Contacts", rid=f"{pkg}:id/title"),
        n([40, 220, 800, 300], text="Bao Lam", rid=f"{pkg}:id/name"),
        n([820, 820, 940, 940], desc="Create contact", rid=f"{pkg}:id/fab"),
    ]
    form_unsaved = [
        n([40, 60, 400, 120], text="Create contact", rid=f"{pkg}:id/form_title"),
        n([50, 200, 950, 290], text="Bao", hint="First name", cls="android.widget.EditText", rid=f"{pkg}:id/editors"),
        n([50, 320, 950, 410], text="Lam", hint="Last name", cls="android.widget.EditText", rid=f"{pkg}:id/editors"),
        n([800, 60, 960, 130], text="Save", rid=f"{pkg}:id/save_btn"),
    ]

    R = lambda els: RawScreen(els, 1000, 1000)
    nf_unsaved = normalize(form_unsaved, width=1000, height=1000)
    nf_saved = normalize(home_saved, width=1000, height=1000)

    # Verification helper ignores EditText values:
    assert Autopilot._values_on_screen(nf_unsaved, ["Bao", "Lam"]) == set()
    assert Autopilot._values_on_screen(nf_saved, ["Bao", "Lam"]) == {"Bao", "Lam"}

    # Workflow where slot1 was typed earlier, now on form_unsaved with NO active
    # outgoing edges (e.g., Save edge was disabled/provisional). Must NOT finish or confirm.
    disabled_save = Edge(
        "e_save_disabled",
        "n_form",
        "n_home",
        "nav",
        tool="click",
        target=TargetDescriptor(text="Save", resource_id=f"{pkg}:id/save_btn"),
        state="disabled",
    )
    # Active slot edge sits elsewhere and is already marked done in a dummy revisit:
    wf = Workflow(
        workflow_id="wf_contacts",
        package=pkg,
        goal="add contact Bao Lam",
        slots={"slot1": "Bao"},
        nodes={
            "n_form": Node("n_form", [app_fingerprint(nf_unsaved)]),
            "n_home": Node("n_home", [app_fingerprint(nf_saved)]),
        },
        edges=[
            Edge(
                "e_slot1",
                "n_other",
                "n_form",
                "slot",
                slot="slot1",
                tool="input_text",
                target=TargetDescriptor(hint="First name"),
                state="active",
            ),
            disabled_save,
        ],
        start_nodes=["n_home"],
        end_nodes=["n_home"],
    )

    class Driver:
        def __init__(self):
            self.calls = 0

        def current(self):
            return R(form_unsaved)

        async def refresh(self):
            return self.current()

        async def act(self, tool, args, element):
            self.calls += 1
            return True, "ok"

    ap = Autopilot(wf, {"slot1": "Bao"}, verify_polls=1, poll_interval_s=0)
    ap._done.add("e_slot1")  # all active slot steps completed; form still unsaved
    rep = asyncio.run(ap.run(Driver()))
    assert not rep.finished
    assert rep.verified_found == []
    assert "CONFIRMED" not in rep.notice(wf.workflow_id, scrollable=True)

    # Even if an active edge on the form was already done (revisit branch), do not
    # claim finished while the form still shows typed values inside EditText:
    wf.edges[1] = Edge(
        "e_save_active",
        "n_form",
        "n_home",
        "nav",
        tool="click",
        target=TargetDescriptor(text="Save", resource_id=f"{pkg}:id/save_btn"),
        state="active",
    )
    ap2 = Autopilot(wf, {"slot1": "Bao"}, verify_polls=1, poll_interval_s=0)
    ap2._done.update({"e_slot1", "e_save_active"})
    rep2 = asyncio.run(ap2.run(Driver()))
    assert not rep2.finished
    assert "CONFIRMED" not in rep2.notice(wf.workflow_id, scrollable=True)

    ex = MagicMock()
    hook = FlashWorkflowHook(SimpleNamespace(ctx=SimpleNamespace(), executor=ex, goal="add contact Bao"), "replay")
    hook.workflow = wf
    hook._arm_end_guard(rep2)
    ex.arm_workflow_end_guard.assert_not_called()

    # Empty home screen with no editable fields DOES not block legitimate confirmation check
    assert normalize(home_empty, width=1000, height=1000)


def test_l043_executor_recovers_keyboard_hidden_field_via_back_and_unique_hint():
    """L-043: when an indexed form field vanished under the soft keyboard (Son Dao case),
    executor checks unique resource-id vs hint, presses BACK once (never swipe),
    re-locates the visible field, preserves L-037, and keeps hint metadata in compact lists."""
    import asyncio
    from types import SimpleNamespace

    from artemis.mcp.action_executor import McpActionExecutor
    from artemis.mcp.action_types import ActionResult
    from artemis.utils.visualization import format_minimal_list_with_elements

    pkg = "com.example.contacts"
    pre_kb_tree = [
        {"bounds": "[50,200][950,280]", "text": "", "hint": "First name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/editors", "package": pkg, "visible": True, "focusable": True},
        {"bounds": "[50,680][950,760]", "text": "", "hint": "Last name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/editors", "package": pkg, "visible": True, "focusable": True},
    ]
    _, pre_kb_elements, _ = format_minimal_list_with_elements(pre_kb_tree, 1000, 1000, compact=True)
    assert pre_kb_elements[1].get("hint") == "Last name"

    # After typing "Son": keyboard opens (y>=580) and form auto-scrolls so Last name disappears completely
    kb_open_tree = [
        {"bounds": "[0,0][1000,1000]", "text": "", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
        {"bounds": "[40,50][400,110]", "text": "Create contact", "class": "android.widget.TextView", "resource-id": f"{pkg}:id/title", "package": pkg, "visible": True},
        {"bounds": "[800,50][960,120]", "text": "Save", "class": "android.widget.Button", "resource-id": f"{pkg}:id/save", "package": pkg, "visible": True, "clickable": True},
        {"bounds": "[50,140][950,220]", "text": "Son", "hint": "First name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/editors", "package": pkg, "visible": True, "focused": True},
        {"bounds": "[0,580][1000,1000]", "text": "", "class": "android.widget.FrameLayout", "package": "com.google.android.inputmethod.latin", "visible": True},
    ]
    # Post-BACK screen: keyboard closed, Last name reappears at new position [50, 520, 950, 600]
    after_back_tree = [
        {"bounds": "[50,180][950,260]", "text": "Son", "hint": "First name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/editors", "package": pkg, "visible": True},
        {"bounds": "[50,520][950,600]", "text": "", "hint": "Last name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/editors", "package": pkg, "visible": True},
    ]
    after_back_xml, after_back_elements, _ = format_minimal_list_with_elements(after_back_tree, 1000, 1000, compact=True)

    ctx = SimpleNamespace(
        last_raw_observation={"elements": kb_open_tree, "width": 1000, "height": 1000, "seq": 1},
        data_engine=None,
        device=SimpleNamespace(device_width=1000, device_height=1000),
    )

    class FakeSession:
        started = True

        def __init__(self):
            self.calls = []
            self.seq = 1

        async def call(self, name, args):
            self.calls.append((name, dict(args)))
            if name == "press_key" and args.get("key") == "BACK":
                self.seq += 1
                ctx.last_raw_observation = {
                    "elements": after_back_tree,
                    "width": 1000,
                    "height": 1000,
                    "seq": self.seq,
                }
            return ActionResult.success(name, "ok")

        async def observe(self, settle_ms=0, include_image=False, persist=True):
            return SimpleNamespace(
                ok=True,
                hierarchy_ok=True,
                elements=after_back_elements,
                elements_text=after_back_xml,
                width=1000,
                height=1000,
                screenshot_path=None,
                scrollable=False,
                message="ok",
                timed_out=False,
            )

    ex = McpActionExecutor(ctx, actuator=SimpleNamespace(extensions=lambda: (), controller=None), agent_name="flash")
    ex._session = FakeSession()
    ex._typed_values = ["son"]
    state = SimpleNamespace(
        indexed_elements=pre_kb_elements,
        indexed_points=[el["center"] for el in pre_kb_elements],
        latest_screenshot=None,
        screen_scrollable=False,
    )

    res = asyncio.run(
        ex.execute("input_text", {"target": 2, "text": "Dao"}, "tc-last", state, index_elements=pre_kb_elements)
    )
    assert res.status == "success"
    assert [n for n, _ in ex._session.calls] == ["press_key", "input_text"]
    assert ex._session.calls[0][1] == {"key": "BACK"}
    assert ex._session.calls[1][1]["target"] == [500, 560]  # new center of Last name, shared rid disambiguated via hint
    assert res.metadata["recovered_screen_changed"] is True
    assert res.metadata["recovered_pre_observation"]["seq"] == 2

    # L-037 must still block overwriting a field whose visible text already holds a typed value:
    after_back_tree[1]["text"] = "Dao"
    ex._session.calls.clear()
    ex.ctx.last_raw_observation = {"elements": kb_open_tree, "width": 1000, "height": 1000, "seq": 5}
    res_l037 = asyncio.run(
        ex.execute("input_text", {"target": 2, "text": "OtherName"}, "tc-l037", state, index_elements=pre_kb_elements)
    )
    assert res_l037.status == "error"
    assert "already holds" in res_l037.text_summary
    assert [n for n, _ in ex._session.calls] == ["press_key"]  # input_text itself was NOT sent


def test_l043_burst_stops_and_recorder_gets_post_back_pre_screen(tmp_path):
    """L-043: when BACK fired mid-burst to recover a hidden field, FlashRunner marks
    has_waited=True so subsequent stale-index calls stop (AGENTS 2.4), and wf_hook
    receives the post-BACK screen as pre."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from langchain_core.messages import ToolMessage

    from artemis.agents.flash.runner import FlashRunner, _TurnRecord
    from artemis.agents.flash.workflow_hook import FlashWorkflowHook
    from artemis.mcp.action_executor import McpActionExecutor
    from artemis.mcp.action_types import ActionResult
    from artemis.utils.visualization import format_minimal_list_with_elements

    pkg = "com.example.contacts"
    pre_kb_tree = [
        {"bounds": "[50,200][950,280]", "text": "", "hint": "First name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/editors", "package": pkg, "visible": True},
        {"bounds": "[50,680][950,760]", "text": "", "hint": "Last name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/editors", "package": pkg, "visible": True},
        {"bounds": "[800,50][960,120]", "text": "Save", "class": "android.widget.Button", "resource-id": f"{pkg}:id/save", "package": pkg, "visible": True, "clickable": True},
    ]
    _, pre_kb_elements, _ = format_minimal_list_with_elements(pre_kb_tree, 1000, 1000, compact=True)
    kb_open_tree = [
        {"bounds": "[0,0][1000,1000]", "text": "", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
        {"bounds": "[40,50][400,110]", "text": "Create contact", "class": "android.widget.TextView", "resource-id": f"{pkg}:id/title", "package": pkg, "visible": True},
        {"bounds": "[800,50][960,120]", "text": "Save", "class": "android.widget.Button", "resource-id": f"{pkg}:id/save", "package": pkg, "visible": True, "clickable": True},
        {"bounds": "[50,140][950,220]", "text": "Son", "hint": "First name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/editors", "package": pkg, "visible": True, "focused": True},
        {"bounds": "[0,580][1000,1000]", "text": "", "class": "android.widget.FrameLayout", "package": "com.google.android.inputmethod.latin", "visible": True},
    ]
    after_back_tree = [
        {"bounds": "[50,180][950,260]", "text": "Son", "hint": "First name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/editors", "package": pkg, "visible": True},
        {"bounds": "[50,520][950,600]", "text": "", "hint": "Last name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/editors", "package": pkg, "visible": True},
        {"bounds": "[800,50][960,120]", "text": "Save", "class": "android.widget.Button", "resource-id": f"{pkg}:id/save", "package": pkg, "visible": True, "clickable": True},
    ]
    after_back_xml, after_back_elements, _ = format_minimal_list_with_elements(after_back_tree, 1000, 1000, compact=True)

    ctx = SimpleNamespace(
        device_serial="emu-1",
        last_raw_observation={"elements": kb_open_tree, "width": 1000, "height": 1000, "seq": 10},
        data_engine=None,
        device=SimpleNamespace(device_width=1000, device_height=1000),
        workflow_store=str(tmp_path),
    )

    class FakeSession:
        started = True

        def __init__(self):
            self.calls = []
            self.seq = 10

        async def call(self, name, args):
            self.calls.append((name, dict(args)))
            if name == "press_key" and args.get("key") == "BACK":
                self.seq = 11
                ctx.last_raw_observation = {"elements": after_back_tree, "width": 1000, "height": 1000, "seq": 11}
            elif name == "input_text":
                self.seq = 12
                ctx.last_raw_observation = {"elements": after_back_tree, "width": 1000, "height": 1000, "seq": 12}
            return ActionResult.success(name, "ok")

        async def observe(self, settle_ms=0, include_image=False, persist=True):
            return SimpleNamespace(
                ok=True,
                hierarchy_ok=True,
                elements=after_back_elements,
                elements_text=after_back_xml,
                width=1000,
                height=1000,
                screenshot_path=None,
                scrollable=False,
                message="ok",
                timed_out=False,
            )

    ex = McpActionExecutor(ctx, actuator=SimpleNamespace(extensions=lambda: (), controller=None), agent_name="flash")
    ex._session = FakeSession()
    ex._typed_values = ["son"]

    runner = FlashRunner.__new__(FlashRunner)
    runner.ctx = ctx
    runner.controller = MagicMock()
    runner.executor = ex
    runner.goal = "add Son Dao"
    runner.fast_burst = True
    runner.summarizer = None
    runner._capture_post_screenshot = AsyncMock(return_value=b"post")
    runner._record_action_step = MagicMock(return_value=1)
    runner._rebase_screen_baseline = MagicMock()
    runner._raise_if_device_offline = lambda **_kw: None
    runner._record_observation_in_transcript = MagicMock()
    runner._wf = FlashWorkflowHook(runner, "record")

    state = SimpleNamespace(
        indexed_elements=pre_kb_elements,
        indexed_points=[el["center"] for el in pre_kb_elements],
        latest_screenshot=None,
        screen_scrollable=False,
    )
    messages = []
    turn = _TurnRecord()

    asyncio.run(
        runner._process_tool_calls(
            [
                {"name": "input_text", "args": {"target": 2, "text": "Dao"}, "id": "tc-1"},
                {"name": "click", "args": {"target": 3}, "id": "tc-2"},
            ],
            state,
            messages,
            "thought",
            {},
            b"pre",
            "xml",
            0,
            turn,
        )
    )

    # Burst stops before click(3) because BACK altered the screen:
    assert [n for n, _ in ex._session.calls] == ["press_key", "input_text"]
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 2
    assert tool_msgs[0].status == "success"
    assert tool_msgs[1].status == "error"
    assert "targets an index or coordinate from an earlier screen" in tool_msgs[1].content
    # Workflow recorder received the post-BACK screen (seq 11) as pre for input_text:
    assert len(runner._wf.steps) == 1
    assert runner._wf.steps[0].pre is not None
    assert runner._wf.steps[0].pre.seq == 11


def test_l045_prefix_remainder_rejected_by_autopilot_and_warned_by_executor():
    """L-045: pre-existing prefix like '+1' making '+10916600066' must NOT pass
    autopilot _verified / _held_slots, formatting noise ('-'/' '/'()'/'.') passes,
    and executor warns clearly when live field text diverges after input_text."""
    import asyncio
    from types import SimpleNamespace

    from artemis.mcp.action_executor import McpActionExecutor
    from artemis.mcp.action_types import ActionResult
    from artemis.utils.visualization import format_minimal_list_with_elements
    from artemis.workflow.autopilot import Autopilot
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.recorder import RawScreen
    from artemis.workflow.store import Edge, Node, Workflow
    from artemis.workflow.targets import TargetDescriptor, descriptor_for

    pkg = "com.example.contacts"
    phone_target = TargetDescriptor(hint="Phone", resource_id=f"{pkg}:id/phone")

    def form_with_phone(phone_val: str):
        return [
            {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
            {"bounds": "[40,50][400,110]", "text": "Create contact", "class": "android.widget.TextView", "resource-id": f"{pkg}:id/title", "package": pkg, "visible": True},
            {"bounds": "[50,300][950,400]", "text": phone_val, "hint": "Phone", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/phone", "package": pkg, "visible": True, "editable": True},
            {"bounds": "[800,50][960,120]", "text": "Save", "class": "android.widget.Button", "resource-id": f"{pkg}:id/save", "package": pkg, "visible": True, "clickable": True},
        ]

    R = lambda els: RawScreen(els, 1000, 1000)
    fp_form = app_fingerprint(normalize(form_with_phone("+1"), 1000, 1000))
    wf = Workflow(
        workflow_id=f"{pkg}__l045",
        package=pkg,
        goal="add contact 0916600066",
        slots={"slot1": "0916600066"},
        nodes={"n1": Node("n1", [fp_form])},
        edges=[
            Edge("e1", "n1", "n1", "slot", slot="slot1", tool="input_text", target=phone_target),
        ],
    )

    ap = Autopilot(wf, {"slot1": "0916600066"}, verify_polls=1, poll_interval_s=0)
    # App-supplied '+1' remained -> '+10916600066' must fail exact normalized verification & held check:
    norm_bad = normalize(form_with_phone("+10916600066"), 1000, 1000)
    assert not ap._verified(wf.edges[0], norm_bad, fp_form, "0916600066")
    assert "slot1" not in ap._held_slots("n1", norm_bad)

    # Punctuation / whitespace formatting alone still passes:
    for ok_fmt in ("0916-600-066", "(091) 660.0066", "0916 600 066"):
        norm_ok = normalize(form_with_phone(ok_fmt), 1000, 1000)
        assert ap._verified(wf.edges[0], norm_ok, fp_form, "0916600066")
        assert "slot1" in ap._held_slots("n1", norm_ok)

    # Full Autopilot run surfaces actual field value in stop_reason when append leaves '+1':
    class MismatchDriver:
        def __init__(self):
            self.stage = 0

        def current(self):
            return R(form_with_phone("+1" if self.stage == 0 else "+10916600066"))

        async def refresh(self):
            return self.current()

        async def act(self, tool, args, element):
            self.stage = 1
            return True, "ok"

    rep = asyncio.run(ap.run(MismatchDriver()))
    assert not rep.finished
    assert "'+10916600066'" in rep.stop_reason and "'0916600066'" in rep.stop_reason

    # Executor (part b) reports clear mismatch warning after AI input_text:
    pre_tree = form_with_phone("+1")
    _, pre_els, _ = format_minimal_list_with_elements(pre_tree, 1000, 1000, compact=True)
    phone_idx = next(el["index"] for el in pre_els if el.get("resource_id") == f"{pkg}:id/phone")
    post_tree = form_with_phone("+10916600066")
    post_xml, post_els, _ = format_minimal_list_with_elements(post_tree, 1000, 1000, compact=True)
    ctx = SimpleNamespace(
        last_raw_observation={"elements": pre_tree, "width": 1000, "height": 1000, "seq": 1},
        data_engine=None,
        device=SimpleNamespace(device_width=1000, device_height=1000),
    )

    class FakeSession:
        started = True

        async def call(self, name, args):
            return ActionResult.success(name, f"input_text typed '{args.get('text')}'")

        async def observe(self, settle_ms=0, include_image=False, persist=True):
            ctx.last_raw_observation = {"elements": post_tree, "width": 1000, "height": 1000, "seq": 2}
            return SimpleNamespace(
                ok=True,
                hierarchy_ok=True,
                elements=post_els,
                elements_text=post_xml,
                width=1000,
                height=1000,
                screenshot_path=None,
                scrollable=False,
                message="ok",
                timed_out=False,
            )

    ex = McpActionExecutor(ctx, actuator=SimpleNamespace(extensions=lambda: (), controller=None), agent_name="flash")
    ex._session = FakeSession()
    state = SimpleNamespace(
        indexed_elements=pre_els,
        indexed_points=[el["center"] for el in pre_els],
        latest_screenshot=None,
        screen_scrollable=False,
    )
    res = asyncio.run(
        ex.execute(
            "input_text",
            {"target": phone_idx, "text": "0916600066", "clear_exist": False},
            "tc-phone",
            state,
            index_elements=pre_els,
        )
    )
    assert res.status == "error"
    assert "field holds '+10916600066', requested '0916600066'" in res.text_summary
    assert "clear_exist=true" in res.text_summary
    assert res.metadata["typed_value_mismatch"] == {"actual": "+10916600066", "expected": "0916600066"}


def test_l046_app_version_change_downgrades_nav_and_prunes_stale_nodes():
    """L-046: versionName change downgrades active nav edges to provisional (and requires
    2 runs to re-activate, even without commitment/judgement), keeps slot edges active,
    and prunes unprotected nodes missing > 10 consecutive runs while protecting autopilot-visited
    and start/end nodes."""
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.recorder import RawScreen, RecordedStep, record_run
    from artemis.workflow.store import Edge, Node, Workflow
    from artemis.workflow.targets import TargetDescriptor, descriptor_for

    pkg = "com.example.contacts"
    home = [
        {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
        {"bounds": "[40,50][400,110]", "text": "Contacts", "class": "android.widget.TextView", "resource-id": f"{pkg}:id/title", "package": pkg, "visible": True},
        {"bounds": "[800,800][950,950]", "content-desc": "Add", "class": "android.widget.ImageButton", "resource-id": f"{pkg}:id/fab", "package": pkg, "visible": True, "clickable": True},
    ]
    form_old = [
        {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
        {"bounds": "[40,50][400,110]", "text": "Old Form", "class": "android.widget.TextView", "resource-id": f"{pkg}:id/old_title", "package": pkg, "visible": True},
        {"bounds": "[50,250][950,350]", "text": "", "hint": "First name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/first", "package": pkg, "visible": True, "editable": True},
    ]
    form_new = [
        {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
        {"bounds": "[40,50][400,110]", "text": "Create contact", "class": "android.widget.TextView", "resource-id": f"{pkg}:id/new_title", "package": pkg, "visible": True},
        {"bounds": "[50,300][950,400]", "text": "", "hint": "Name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/name", "package": pkg, "visible": True, "editable": True},
        {"bounds": "[800,50][960,120]", "text": "Save", "class": "android.widget.Button", "resource-id": f"{pkg}:id/save", "package": pkg, "visible": True, "clickable": True},
    ]
    done_screen = [
        {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
        {"bounds": "[40,50][400,110]", "text": "Contact saved", "class": "android.widget.TextView", "resource-id": f"{pkg}:id/saved_title", "package": pkg, "visible": True},
        {"bounds": "[50,250][700,320]", "text": "An", "class": "android.widget.TextView", "resource-id": f"{pkg}:id/saved_name", "package": pkg, "visible": True},
    ]
    R = lambda els: RawScreen(els, 1000, 1000)
    norm_home = normalize(home, 1000, 1000)
    fp_home = app_fingerprint(norm_home)
    fp_old = app_fingerprint(normalize(form_old, 1000, 1000))
    fp_new = app_fingerprint(normalize(form_new, 1000, 1000))
    fp_done = app_fingerprint(normalize(done_screen, 1000, 1000))
    fab_desc = descriptor_for(next(n for n in norm_home if n.get("resource-id") == f"{pkg}:id/fab"))

    wf = Workflow(
        workflow_id=f"{pkg}__1",
        package=pkg,
        goal="add An",
        slots={"slot1": "An"},
        nodes={
            "n_home": Node("n_home", [fp_home]),
            "n_old": Node("n_old", [fp_old]),
            "n_new": Node("n_new", [fp_new]),
            "n_done": Node("n_done", [fp_done]),
        },
        edges=[
            Edge("e_nav", "n_home", "n_new", "nav", tool="click", target=fab_desc, seen=4, state="active"),
            Edge("e_slot", "n_new", "n_new", "slot", slot="slot1", tool="input_text", target=TargetDescriptor(resource_id=f"{pkg}:id/name", hint="Name"), seen=4, state="active"),
            Edge("e_old", "n_old", "n_done", "nav", tool="click", target=TargetDescriptor(resource_id=f"{pkg}:id/old_title"), seen=2, state="active"),
        ],
        start_nodes=["n_home"],
        end_nodes=["n_done"],
        app_version="4.5.0",
    )

    # 1. Version bump downgrades active nav edges to provisional with seen=0, preserves slot edge:
    assert wf.apply_app_version("4.92.0") is True
    assert wf.app_version == "4.92.0"
    assert wf.edges[0].state == "provisional" and wf.edges[0].seen == 0
    assert wf.edges[1].state == "active"  # slot stays active
    assert wf.edges[2].state == "provisional" and wf.edges[2].seen == 0
    assert wf.apply_app_version("") is False  # unknown version is a no-op

    # Roundtrip preserves app_version and miss_count
    wf.nodes["n_old"].miss_count = 3
    reloaded = Workflow.from_json(wf.to_json())
    assert reloaded.app_version == "4.92.0"
    assert reloaded.nodes["n_old"].miss_count == 3

    # 2. Recording 2 independent runs on 4.92.0 re-activates e_nav (first stays provisional, second activates)
    # Also note n_new is visited via autopilot=True step -> miss_count must stay 0;
    # n_old is never visited across 11 runs -> pruned after > 10 runs, removing e_old, keeping n_home/n_done.
    nav_step = RecordedStep(
        tool="click",
        args={"target": 1},
        action={"action": "click", "normalized_coordinates": [875, 875], "target_resource_id": f"{pkg}:id/fab", "target_bounds": [800, 800, 950, 950]},
        ok=True,
        pre=R(home),
        post=R(form_new),
        autopilot=False,
    )
    auto_slot_step = RecordedStep(
        tool="input_text",
        args={"target": 1, "text": "An"},
        action={"action": "input_text", "normalized_coordinates": [500, 350]},
        ok=True,
        pre=R(form_new),
        post=R(form_new),
        autopilot=True,
    )
    reloaded.nodes["n_old"].miss_count = 0
    for run_idx in range(1, 12):
        res_wf = record_run(
            [nav_step, auto_slot_step],
            goal="add An",
            workflow=reloaded,
            slot_values={"slot1": "An"},
            app_version="4.92.0",
        )
        assert res_wf is reloaded
        assert reloaded.nodes["n_new"].miss_count == 0
        nav_edge = next(e for e in reloaded.edges if e.id == "e_nav")
        if run_idx == 1:
            assert nav_edge.state == "provisional" and nav_edge.seen == 1
        else:
            assert nav_edge.state == "active" and nav_edge.seen >= 2
        if run_idx <= 10:
            assert "n_old" in reloaded.nodes
            assert reloaded.nodes["n_old"].miss_count == run_idx
        else:
            assert "n_old" not in reloaded.nodes
            assert all(e.src != "n_old" and e.dst != "n_old" for e in reloaded.edges)
            assert "n_home" in reloaded.nodes and "n_done" in reloaded.nodes


def test_l047_composite_slot_recorded_and_replayed_in_goal_order():
    """L-047: single Name field holding 'Nam Kha' from separate slots slot1='Nam', slot2='Kha'
    in goal order is recorded as composite slot 'slot1+slot2', does not guess wrong orders or
    non-space separators, and replays cleanly with new slot values."""
    import asyncio
    from types import SimpleNamespace

    from artemis.workflow.autopilot import Autopilot
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.playbook import format_step
    from artemis.workflow.recorder import RawScreen, RecordedStep, record_run
    from artemis.workflow.store import Edge, Node, Workflow
    from artemis.workflow.targets import TargetDescriptor, descriptor_for

    pkg = "com.example.contacts"
    form_new = lambda name_val="", phone_val="": [
        {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
        {"bounds": "[40,50][400,110]", "text": "Create contact", "class": "android.widget.TextView", "resource-id": f"{pkg}:id/title", "package": pkg, "visible": True},
        {"bounds": "[50,200][950,290]", "text": name_val, "hint": "Name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/name", "package": pkg, "visible": True, "editable": True},
        {"bounds": "[50,340][950,430]", "text": phone_val, "hint": "Phone", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/phone", "package": pkg, "visible": True, "editable": True},
        {"bounds": "[800,50][960,120]", "text": "Save", "class": "android.widget.Button", "resource-id": f"{pkg}:id/save", "package": pkg, "visible": True, "clickable": True},
    ]
    home_saved = lambda label="", phone_label="": [
        {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
        {"bounds": "[40,50][400,110]", "text": "Contacts", "class": "android.widget.TextView", "resource-id": f"{pkg}:id/home_title", "package": pkg, "visible": True},
        {"bounds": "[40,220][700,300]", "text": label, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/row", "package": pkg, "visible": True},
        *([{"bounds": "[40,320][700,390]", "text": phone_label, "class": "android.widget.TextView", "resource-id": f"{pkg}:id/row_phone", "package": pkg, "visible": True}] if phone_label else []),
    ]
    R = lambda els: RawScreen(els, 1000, 1000)

    # Existing workflow originally recorded with separate First name (slot1) and Last name (slot2) + Phone (slot3)
    old_wf = Workflow(
        workflow_id=f"{pkg}__1",
        package=pkg,
        goal="add contact first name Bao, last name Lam, phone 0900",
        slots={"slot1": "Bao", "slot2": "Lam", "slot3": "0900"},
        nodes={
            "n_old_form": Node("n_old_form", [app_fingerprint(normalize([
                {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
                {"bounds": "[50,200][950,290]", "text": "", "hint": "First name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/fn", "package": pkg, "visible": True, "editable": True},
                {"bounds": "[50,320][950,410]", "text": "", "hint": "Last name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/ln", "package": pkg, "visible": True, "editable": True},
            ], 1000, 1000))]),
        },
        edges=[
            Edge("e_old1", "n_old_form", "n_old_form", "slot", slot="slot1", tool="input_text", target=TargetDescriptor(hint="First name")),
            Edge("e_old2", "n_old_form", "n_old_form", "slot", slot="slot2", tool="input_text", target=TargetDescriptor(hint="Last name")),
        ],
    )

    new_goal = "add contact first name Nam, last name Kha, phone 0916600066"
    new_slots = {"slot1": "Nam", "slot2": "Kha", "slot3": "0916600066"}
    merged = record_run(
        [
            RecordedStep(
                tool="input_text",
                args={"target": 1, "text": "Nam Kha"},
                action={"action": "input_text", "normalized_coordinates": [500, 245]},
                ok=True,
                pre=R(form_new()),
                post=R(form_new("Nam Kha")),
            ),
            RecordedStep(
                tool="input_text",
                args={"target": 2, "text": "0916600066"},
                action={"action": "input_text", "normalized_coordinates": [500, 385]},
                ok=True,
                pre=R(form_new("Nam Kha")),
                post=R(form_new("Nam Kha", "0916600066")),
            ),
            RecordedStep(
                tool="click",
                args={"target": 3},
                action={"action": "click", "normalized_coordinates": [880, 85], "target_resource_id": f"{pkg}:id/save", "target_bounds": [800, 50, 960, 120]},
                ok=True,
                pre=R(form_new("Nam Kha", "0916600066")),
                post=R(home_saved("Nam Kha", "0916600066")),
            ),
        ],
        goal=new_goal,
        workflow=old_wf,
        slot_values=new_slots,
    )
    assert merged is not None
    comp_edge = next(e for e in merged.edges if e.target and e.target.hint == "Name")
    assert comp_edge.kind == "slot" and comp_edge.slot == "slot1+slot2"
    assert "type 'Linh Ta'" in format_step(comp_edge, {"slot1": "Linh", "slot2": "Ta"})

    # Reversed order ('Kha Nam') or non-space separator ('Nam-Kha') must NOT be treated as composite slot:
    for bad_text in ("Kha Nam", "Nam-Kha"):
        neg_wf = Workflow(workflow_id=f"{pkg}__neg", package=pkg, goal=new_goal, slots=old_wf.slots)
        res_neg = record_run(
            [
                RecordedStep(
                    tool="input_text",
                    args={"target": 1, "text": bad_text},
                    action={"action": "input_text", "normalized_coordinates": [500, 245]},
                    ok=True,
                    pre=R(form_new()),
                    post=R(form_new(bad_text)),
                ),
            ],
            goal=new_goal,
            workflow=neg_wf,
            slot_values=new_slots,
        )
        assert res_neg is not None
        assert res_neg.edges[0].kind == "judgement"

    # Replay composite + phone slots automatically with new task values ("Linh Ta"):
    class ReplayDriver:
        def __init__(self):
            self.name = ""
            self.phone = ""
            self.saved = False
            self.typed_actions = []

        def current(self):
            if self.saved:
                return R(home_saved("Linh Ta", "0988777666"))
            return R(form_new(self.name, self.phone))

        async def refresh(self):
            return self.current()

        async def act(self, tool, args, element):
            if tool == "input_text":
                self.typed_actions.append(args["text"])
                if args["text"] == "Linh Ta":
                    self.name = "Linh Ta"
                elif args["text"] == "0988777666":
                    self.phone = "0988777666"
                return True, "ok"
            if tool == "click":
                self.saved = True
                return True, "ok"
            return False, "unexpected"

    drv = ReplayDriver()
    ap = Autopilot(merged, {"slot1": "Linh", "slot2": "Ta", "slot3": "0988777666"}, verify_polls=1, poll_interval_s=0)
    rep = asyncio.run(ap.run(drv))
    assert rep.finished
    assert drv.typed_actions == ["Linh Ta", "0988777666"]
    assert rep.verified_found == ["Linh", "Ta", "0988777666"] and rep.verified_missing == []


def test_l048_prompt_deviation_pauses_autopilot_for_ai_confirmation_and_supports_manual_or_proceed(tmp_path):
    """L-048 part 1: when a new goal carries instructions outside the recorded
    slot template (e.g. 'save to SIM'), maybe_autopilot does NOT execute steps
    blindly; it hands the planned playbook to AI, lets AI proceed via
    workflow_autopilot(decision='proceed') or take manual control directly."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from langchain_core.messages import ToolMessage

    from artemis.agents.flash.runner import FlashRunner, _TurnRecord
    from artemis.agents.flash.workflow_hook import FlashWorkflowHook
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.selector import goal_deviation_summary
    from artemis.workflow.store import Edge, Node, Workflow, WorkflowStore
    from artemis.workflow.targets import TargetDescriptor

    pkg = "com.example.contacts"
    home_tree = [
        {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
        {"bounds": "[40,50][400,110]", "text": "Contacts", "class": "android.widget.TextView", "resource-id": f"{pkg}:id/title", "package": pkg, "visible": True},
        {"bounds": "[800,800][950,950]", "content-desc": "Add", "class": "android.widget.ImageButton", "resource-id": f"{pkg}:id/fab", "package": pkg, "visible": True, "clickable": True},
    ]
    form_tree = [
        {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
        {"bounds": "[50,200][950,290]", "text": "", "hint": "Name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/name", "package": pkg, "visible": True, "editable": True},
    ]
    fp_home = app_fingerprint(normalize(home_tree, 1000, 1000))
    fp_form = app_fingerprint(normalize(form_tree, 1000, 1000))
    wf = Workflow(
        workflow_id=f"{pkg}__1",
        package=pkg,
        goal="Open Contacts and create contact Bao Lam",
        slots={"slot1": "Bao", "slot2": "Lam"},
        nodes={"n1": Node("n1", [fp_home]), "n2": Node("n2", [fp_form])},
        edges=[
            Edge("e1", "n1", "n2", "nav", tool="click", label="Add", target=TargetDescriptor(resource_id=f"{pkg}:id/fab", text="Add")),
            Edge("e2", "n2", "n2", "slot", slot="slot1", tool="input_text", target=TargetDescriptor(hint="Name")),
        ],
        start_nodes=["n1"],
        end_nodes=["n2"],
    )

    # Identical framing (only slot values changed) produces no deviation:
    assert goal_deviation_summary(
        "Open Contacts and create contact Tuan Vo",
        wf.goal,
        wf.slots,
        {"slot1": "Tuan", "slot2": "Vo"},
    ) == ""
    assert goal_deviation_summary(
        "add first name Tuan, last name Vo",
        "add first name Bao, last name Lam",
        {"slot1": "Bao", "slot2": "Lam"},
    ) == ""
    # Extra instruction triggers clear summary:
    diff = goal_deviation_summary(
        "Open Contacts and create contact Tuan Vo, save to SIM",
        wf.goal,
        wf.slots,
        {"slot1": "Tuan", "slot2": "Vo"},
    )
    assert "save to SIM" in diff

    store = WorkflowStore(tmp_path)
    store.save(wf)

    def _make_runner(goal_text: str):
        ctx = SimpleNamespace(
            last_raw_observation={"elements": home_tree, "width": 1000, "height": 1000, "seq": 1},
            data_engine=None,
            device=SimpleNamespace(device_width=1000, device_height=1000, device_id="dev1"),
            _workflow_version_cache={("dev1", pkg): ""},
        )
        ex = MagicMock()
        ex.action_tool_names = frozenset({"click", "input_text", "press_key", "swipe"})
        runner = FlashRunner.__new__(FlashRunner)
        runner.ctx = ctx
        runner.goal = goal_text
        runner.executor = ex
        runner.controller = MagicMock()
        runner.fast_burst = True
        runner.summarizer = None
        runner._raise_if_device_offline = lambda **_kw: None
        hook = FlashWorkflowHook(runner, "replay", str(tmp_path))
        hook.workflow = wf
        hook.slot_values = {"slot1": "Tuan", "slot2": "Vo"}
        hook._selected_for = pkg
        from artemis.workflow.autopilot import Autopilot
        hook._autopilot = Autopilot(wf, hook.slot_values)
        runner._wf = hook
        return runner, hook

    # Case A: paused on Turn 1 with Planned workflow steps, then AI approves via workflow_autopilot('proceed')
    runner_a, hook_a = _make_runner("Open Contacts and create contact Tuan Vo, save to SIM")
    state = SimpleNamespace(screen_scrollable=False, indexed_elements=[], indexed_points=[])
    handed = asyncio.run(hook_a.maybe_autopilot(state, b"img", "[1] Add"))
    assert handed is not None
    _, _, notice = handed
    assert "[Workflow Review]" in notice
    assert "save to SIM" in notice
    assert "Planned workflow steps:" in notice
    assert "workflow_autopilot(decision='proceed')" in notice
    # Autopilot did NOT dispatch any device action during review pause:
    assert hook_a.autopilot_steps == 0
    # Wait without decision on next check returns None (no spam / no hijack):
    assert asyncio.run(hook_a.maybe_autopilot(state, b"img", "[1] Add")) is None

    # AI approves -> trailing burst calls get 1-to-1 ToolMessages; _deviation_decision becomes 'proceed':
    msgs_a: list = []
    turn_a = _TurnRecord()
    asyncio.run(
        runner_a._process_tool_calls(
            [
                {"name": "workflow_autopilot", "args": {"decision": "proceed", "reason": "steps match"}, "id": "tc-wf"},
                {"name": "click", "args": {"target": 1}, "id": "tc-skip"},
            ],
            state,
            msgs_a,
            "thought",
            {},
            b"img",
            "xml",
            0,
            turn_a,
        )
    )
    tool_msgs = [m for m in msgs_a if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 2
    assert tool_msgs[0].status == "success"
    assert tool_msgs[1].status == "error"
    assert hook_a._deviation_decision == "proceed"

    # Case B: AI acts directly on screen instead of calling workflow_autopilot -> auto locks manual mode:
    runner_b, hook_b = _make_runner("Open Contacts and create contact Tuan Vo, save to SIM")
    handed_b = asyncio.run(hook_b.maybe_autopilot(state, b"img", "[1] Add"))
    assert handed_b is not None and hook_b._deviation_decision == "prompted"
    hook_b.note_ai_manual_override()
    assert hook_b._deviation_decision == "manual"
    assert asyncio.run(hook_b.maybe_autopilot(state, b"img", "[1] Add")) is None


def test_l048_per_task_workflow_override_in_mcp_and_resolve_mode(tmp_path, monkeypatch):
    """L-048 part 2: per-task workflow='off'|'record'|'replay' overrides config/env via
    ctx.workflow_mode and mobile_run_task validates and forwards it to both daemon and standalone."""
    import os
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    import pytest

    from artemis.agents.flash.workflow_hook import resolve_mode
    from artemis.runtime import trace_store
    from mcp_server.tools import task_runner

    # ctx.workflow_mode beats env and config
    monkeypatch.setenv("ARTEMIS_WORKFLOW_MODE", "replay")
    cfg = SimpleNamespace(mode="replay", dir=None, max_steps=15)
    assert resolve_mode(cfg, SimpleNamespace(workflow_mode="off"))[0] == "off"
    assert resolve_mode(cfg, None)[0] == "replay"

    # Rejects unknown workflow mode cleanly before trace init:
    with pytest.raises(ValueError, match="Invalid workflow"):
        task_runner.mobile_run_task(task_desc="test", model="Flash", workflow="bogus")

    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    monkeypatch.delenv("ARTEMIS_STANDALONE", raising=False)

    daemon_submit = MagicMock(side_effect=lambda **kw: {"tasks": [{"session_id": kw["session_id"]}]})
    with (
        patch.object(task_runner, "ensure_daemon_running", return_value=(True, "http://127.0.0.1:9999")),
        patch.object(task_runner, "submit_task_to_daemon", daemon_submit),
    ):
        task_runner.mobile_run_task(task_desc="Manual run", model="Flash", workflow="off")
    assert daemon_submit.call_args.kwargs["workflow"] == "off"

    popen_mock = MagicMock()
    popen_mock.return_value.pid = os.getpid()
    lock_cls_mock = MagicMock()
    lock_cls_mock.reserve.return_value = "ticket-wf"
    lock_cls_mock.QUEUE_TICKET_ENV = "ARTEMIS_QUEUE_TICKET"
    with (
        patch.object(task_runner, "ensure_daemon_running", return_value=(False, None)),
        patch.object(task_runner, "DeviceExecutionLock", lock_cls_mock),
        patch.object(task_runner.subprocess, "Popen", popen_mock),
        patch.object(task_runner.threading, "Thread", return_value=MagicMock()),
        patch.object(task_runner, "read_ipc_port", return_value=0),
    ):
        task_runner.mobile_run_task(task_desc="Standalone manual", model="Flash", workflow="off")
    cmd = popen_mock.call_args.args[0]
    env = popen_mock.call_args.kwargs["env"]
    assert "--workflow" in cmd and cmd[cmd.index("--workflow") + 1] == "off"
    assert env["ARTEMIS_WORKFLOW_MODE"] == "off"


def test_l049_password_field_masks_and_accent_preservation():
    """L-049: (1) password=true fields verify by masked char count ('••••' == len('pass')),
    never warn when masked count matches or empty/unverifiable;
    (2) field normalization keeps Vietnamese accents (NFC) so 'Ha Noi' != 'Hà Nội'."""
    import asyncio
    from types import SimpleNamespace

    from artemis.mcp.action_executor import McpActionExecutor
    from artemis.mcp.action_types import ActionResult
    from artemis.utils.visualization import format_minimal_list_with_elements
    from artemis.workflow.autopilot import Autopilot
    from artemis.workflow.fingerprint import app_fingerprint, normalize
    from artemis.workflow.recorder import RawScreen
    from artemis.workflow.store import Edge, Node, Workflow
    from artemis.workflow.targets import TargetDescriptor, normalize_field_value

    # 1) Accent preservation (NFC vs NFD vs ASCII drop)
    assert normalize_field_value("Hà Nội") != normalize_field_value("Ha Noi")
    assert normalize_field_value("Hà Nội") == normalize_field_value("hà nội")
    assert normalize_field_value("Hà Nội") == normalize_field_value("Ha\u0300 No\u0302\u0323i")

    pkg = "com.example.login"
    pw_target = TargetDescriptor(hint="Password", resource_id=f"{pkg}:id/pw")
    city_target = TargetDescriptor(hint="City", resource_id=f"{pkg}:id/city")

    def screen_tree(*, pw_text: str = "", city_text: str = ""):
        return [
            {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
            {"bounds": "[50,200][950,290]", "text": pw_text, "hint": "Password", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/pw", "package": pkg, "visible": True, "editable": True, "password": True},
            {"bounds": "[50,340][950,430]", "text": city_text, "hint": "City", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/city", "package": pkg, "visible": True, "editable": True, "password": False},
        ]

    R = lambda els: RawScreen(els, 1000, 1000)
    fp = app_fingerprint(normalize(screen_tree(), 1000, 1000))
    wf = Workflow(
        workflow_id=f"{pkg}__1",
        package=pkg,
        goal="login",
        slots={"slot1": "pass", "slot2": "Hà Nội"},
        nodes={"n1": Node("n1", [fp])},
        edges=[
            Edge("e_pw", "n1", "n1", "slot", slot="slot1", tool="input_text", target=pw_target),
            Edge("e_city", "n1", "n1", "slot", slot="slot2", tool="input_text", target=city_target),
        ],
    )
    ap = Autopilot(wf, {"slot1": "pass", "slot2": "Hà Nội"}, verify_polls=1, poll_interval_s=0)

    # Password mask matches password length -> verified True!
    n_pw_ok = normalize(screen_tree(pw_text="••••"), 1000, 1000)
    assert ap._verified(wf.edges[0], n_pw_ok, fp, "pass")
    assert "slot1" in ap._held_slots("n1", n_pw_ok)

    # Password mask wrong length -> verified False
    n_pw_bad = normalize(screen_tree(pw_text="••••••"), 1000, 1000)
    assert not ap._verified(wf.edges[0], n_pw_bad, fp, "pass")

    # Empty password field (not verifiable) -> verified False, AND Autopilot does NOT emit misleading mismatch
    class EmptyPwDriver:
        def current(self):
            return R(screen_tree(pw_text=""))

        async def refresh(self):
            return self.current()

        async def act(self, tool, args, element):
            return True, "ok"

    rep_pw = asyncio.run(ap.run(EmptyPwDriver()))
    assert not rep_pw.finished
    assert "field holds" not in rep_pw.stop_reason

    # City field losing Vietnamese accents ('Ha Noi' != 'Hà Nội') -> rejected by autopilot
    n_city_lost = normalize(screen_tree(city_text="Ha Noi"), 1000, 1000)
    n_city_ok = normalize(screen_tree(city_text="Hà Nội"), 1000, 1000)
    assert not ap._verified(wf.edges[1], n_city_lost, fp, "Hà Nội")
    assert ap._verified(wf.edges[1], n_city_ok, fp, "Hà Nội")

    # Executor checks: password masked ('••••') and empty password ('') must NOT warn;
    # city 'Ha Noi' vs 'Hà Nội' MUST warn.
    pre_tree = screen_tree()
    _, pre_els, _ = format_minimal_list_with_elements(pre_tree, 1000, 1000, compact=True)
    pw_idx = next(el["index"] for el in pre_els if el.get("resource_id") == f"{pkg}:id/pw")
    city_idx = next(el["index"] for el in pre_els if el.get("resource_id") == f"{pkg}:id/city")

    post_tree_state = {"tree": pre_tree}

    class FakeSession:
        started = True

        async def call(self, name, args):
            return ActionResult.success(name, "ok")

        async def observe(self, settle_ms=0, include_image=False, persist=True):
            t = post_tree_state["tree"]
            xml, els, _ = format_minimal_list_with_elements(t, 1000, 1000, compact=True)
            ctx.last_raw_observation = {"elements": t, "width": 1000, "height": 1000, "seq": 2}
            return SimpleNamespace(
                ok=True, hierarchy_ok=True, elements=els, elements_text=xml,
                width=1000, height=1000, screenshot_path=None, scrollable=False, message="ok", timed_out=False,
            )

    ctx = SimpleNamespace(
        last_raw_observation={"elements": pre_tree, "width": 1000, "height": 1000, "seq": 1},
        data_engine=None,
        device=SimpleNamespace(device_width=1000, device_height=1000),
    )
    ex = McpActionExecutor(ctx, actuator=SimpleNamespace(extensions=lambda: (), controller=None), agent_name="flash")
    ex._session = FakeSession()
    state = SimpleNamespace(indexed_elements=pre_els, indexed_points=[el["center"] for el in pre_els], latest_screenshot=None, screen_scrollable=False)

    # Password returns '••••' for len 4 -> no warning!
    post_tree_state["tree"] = screen_tree(pw_text="••••")
    res_pw_mask = asyncio.run(ex.execute("input_text", {"target": pw_idx, "text": "pass"}, "tc-pw1", state, index_elements=pre_els))
    assert "Warning:" not in res_pw_mask.text_summary
    assert "typed_value_mismatch" not in res_pw_mask.metadata

    # Password returns empty '' -> unverifiable -> no warning!
    post_tree_state["tree"] = screen_tree(pw_text="")
    ex._last_point_action = None
    ex._ineffective_taps = []
    res_pw_empty = asyncio.run(ex.execute("input_text", {"target": pw_idx, "text": "pass"}, "tc-pw2", state, index_elements=pre_els))
    assert "Warning:" not in res_pw_empty.text_summary
    assert "typed_value_mismatch" not in res_pw_empty.metadata

    # City typed 'Hà Nội' but ADB dropped accents to 'Ha Noi' -> fails with clear hint!
    post_tree_state["tree"] = screen_tree(city_text="Ha Noi")
    res_city = asyncio.run(ex.execute("input_text", {"target": city_idx, "text": "Hà Nội"}, "tc-city", state, index_elements=pre_els))
    assert res_city.status == "error"
    assert "field holds 'Ha Noi', requested 'Hà Nội'" in res_city.text_summary


def test_l050_executor_refuses_input_text_on_non_field_and_labels_unlabeled_inputs():
    """L-050: (1) compact list keeps unlabeled EditText even when a static TextView label
    sits inside it (Company) and labels unlabeled input fields as '(input field: \"...\")'
    from nearby XML/OCR text; (2) executor refuses input_text(target=i) aimed at a button
    instead of an input field, while still allowing OCR/hint targets."""
    import asyncio
    from types import SimpleNamespace

    from artemis.mcp.action_executor import McpActionExecutor
    from artemis.mcp.action_types import ActionResult
    from artemis.utils.visualization import format_minimal_list_with_elements, merge_ocr_into_list

    pkg = "com.example.contacts"
    # Mirrors 325ba852 Create contact screen:
    # - Button [100,150][350,250] (avatar button) -> (button)
    # - Unlabeled EditText [100,600][620,680] with static TextView "Company" inside its bounds
    # - Unlabeled EditText [100,800][620,880] below an OCR text "Email" at [120,760][220,795]
    nodes = [
        {"bounds": "[100,150][350,250]", "text": "", "class": "android.widget.Button", "package": pkg, "visible": True, "clickable": True},
        {"bounds": "[100,600][620,680]", "text": "", "class": "android.widget.EditText", "package": pkg, "visible": True, "clickable": True, "editable": True},
        {"bounds": "[128,630][267,670]", "text": "Company", "class": "android.widget.TextView", "package": pkg, "visible": True, "clickable": False},
        {"bounds": "[100,800][620,880]", "text": "", "class": "android.widget.EditText", "package": pkg, "visible": True, "clickable": True, "editable": True},
    ]
    txt, els, _ = format_minimal_list_with_elements(nodes, 720, 1280, compact=True)
    ocr_items = [{
        "text": "Email",
        "score": 0.99,
        "position": [{"x": 120, "y": 760}, {"x": 220, "y": 760}, {"x": 220, "y": 795}, {"x": 120, "y": 795}],
    }]
    merged_txt, _ = merge_ocr_into_list(txt, els, ocr_items, 720, 1280)

    assert '(button)' in merged_txt
    assert '(input field: "Company")' in merged_txt
    assert '(input field: "Email")' in merged_txt

    btn_idx = next(el["index"] for el in els if el.get("class") == "android.widget.Button")
    input_idx = next(el["index"] for el in els if el.get("nearby_label") == "Company")
    ocr_idx = next(el["index"] for el in els if el.get("is_ocr"))

    ctx = SimpleNamespace(
        last_raw_observation={"elements": nodes, "width": 720, "height": 1280, "seq": 1},
        data_engine=None,
        device=SimpleNamespace(device_width=720, device_height=1280),
    )

    class FakeSession:
        started = True

        def __init__(self):
            self.calls = []

        async def call(self, name, args):
            self.calls.append((name, dict(args)))
            return ActionResult.success(name, "ok")

        async def observe(self, settle_ms=0, include_image=False, persist=True):
            return SimpleNamespace(
                ok=True, hierarchy_ok=True, elements=els, elements_text=merged_txt,
                width=720, height=1280, screenshot_path=None, scrollable=False, message="ok", timed_out=False,
            )

    ex = McpActionExecutor(ctx, actuator=SimpleNamespace(extensions=lambda: (), controller=None), agent_name="flash")
    ex._session = FakeSession()
    state = SimpleNamespace(indexed_elements=els, indexed_points=[e["center"] for e in els], latest_screenshot=None, screen_scrollable=False)

    # Targeting the avatar button with input_text is refused without dispatching tap/type:
    res_btn = asyncio.run(ex.execute("input_text", {"target": btn_idx, "text": "Dung Mac"}, "tc-btn", state, index_elements=els))
    assert res_btn.status == "error"
    assert "not an input field" in res_btn.text_summary
    assert "without target" in res_btn.text_summary
    assert ex._session.calls == []

    # Targeting real unlabeled input field or OCR label works:
    res_inp = asyncio.run(ex.execute("input_text", {"target": input_idx, "text": "Acme"}, "tc-inp", state, index_elements=els))
    assert res_inp.status == "success"
    res_ocr = asyncio.run(ex.execute("input_text", {"target": ocr_idx, "text": "a@b.com"}, "tc-ocr", state, index_elements=els))
    assert res_ocr.status == "success"
    assert [c[0] for c in ex._session.calls] == ["input_text", "input_text"]


def test_l051_typed_mismatch_fails_and_reminds_once_on_report_completion():
    """L-051: when post-typing field text diverges from expected (e.g. '+10913400134'
    vs '0913400134'), input_text fails with clear instructions while keeping fresh XML,
    and report_task_status(completed) reminds the AI once if left unfixed."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from langchain_core.messages import ToolMessage

    from artemis.agents.flash.runner import FlashRunner, _TurnRecord
    from artemis.mcp.action_executor import McpActionExecutor
    from artemis.mcp.action_types import ActionResult
    from artemis.utils.visualization import format_minimal_list_with_elements

    pkg = "com.example.contacts"

    def phone_tree(val: str):
        return [
            {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
            {"bounds": "[50,300][950,400]", "text": val, "hint": "Phone", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/phone", "package": pkg, "visible": True, "editable": True},
        ]

    pre_tree = phone_tree("+1")
    _, pre_els, _ = format_minimal_list_with_elements(pre_tree, 1000, 1000, compact=True)
    phone_idx = next(e["index"] for e in pre_els if e.get("resource_id") == f"{pkg}:id/phone")
    post_tree_bad = phone_tree("+10913400134")
    bad_xml, bad_els, _ = format_minimal_list_with_elements(post_tree_bad, 1000, 1000, compact=True)

    ctx = SimpleNamespace(
        last_raw_observation={"elements": pre_tree, "width": 1000, "height": 1000, "seq": 1},
        data_engine=None,
        device=SimpleNamespace(device_width=1000, device_height=1000),
    )

    class FakeSession:
        started = True

        async def call(self, name, args):
            return ActionResult.success(name, f"Typed '{args.get('text')}'")

        async def observe(self, settle_ms=0, include_image=False, persist=True):
            ctx.last_raw_observation = {"elements": post_tree_bad, "width": 1000, "height": 1000, "seq": 2}
            return SimpleNamespace(
                ok=True, hierarchy_ok=True, elements=bad_els, elements_text=bad_xml,
                width=1000, height=1000, screenshot_path=None, scrollable=False, message="ok", timed_out=False,
            )

    ex = McpActionExecutor(ctx, actuator=SimpleNamespace(extensions=lambda: (), controller=None), agent_name="flash")
    ex._session = FakeSession()
    state = SimpleNamespace(indexed_elements=pre_els, indexed_points=[e["center"] for e in pre_els], latest_screenshot=None, screen_scrollable=False)

    res = asyncio.run(
        ex.execute("input_text", {"target": phone_idx, "text": "0913400134", "clear_exist": False}, "tc-bad", state, index_elements=pre_els)
    )
    assert res.status == "error"
    assert "+10913400134" in res.text_summary and "clear_exist=true" in res.text_summary
    assert res.ui_elements_text == bad_xml  # updated UI context still delivered

    runner = FlashRunner.__new__(FlashRunner)
    runner.ctx = ctx
    runner.executor = ex
    runner.controller = MagicMock()
    runner.goal = "save contact"
    runner.fast_burst = True
    runner.summarizer = None
    runner._raise_if_device_offline = lambda **_kw: None
    runner._wf = None
    runner._loop_guard = None
    runner._scroll_probes = 99
    runner._finalize_task_report = AsyncMock(return_value={"status": "completed", "explanation": "done"})

    # First report_task_status(completed) without fixing reminds once:
    msgs1: list = []
    report1, _, _, _ = asyncio.run(
        runner._process_tool_calls(
            [{"name": "report_task_status", "args": {"status": "completed", "explanation": "saved"}, "id": "tc-rep1"}],
            state, msgs1, "thought", {}, b"img", bad_xml, 0, _TurnRecord(),
        )
    )
    assert report1 is None
    assert len(msgs1) == 1 and msgs1[0].status == "error"
    assert "+10913400134" in msgs1[0].content and "0913400134" in msgs1[0].content

    # Second call after AI explains / proceeds is allowed through:
    msgs2: list = []
    report2, _, _, _ = asyncio.run(
        runner._process_tool_calls(
            [{"name": "report_task_status", "args": {"status": "completed", "explanation": "explained"}, "id": "tc-rep2"}],
            state, msgs2, "thought", {}, b"img", bad_xml, 0, _TurnRecord(),
        )
    )
    assert report2 == {"status": "completed", "explanation": "done"}


def test_l055_numeric_commas_auto_format_retry_append_and_prefill_failure():
    """L-055: (1) only untyped prefill+typed concats fail; (2) intentional appends onto
    session-typed text are accepted without mismatch; (3) retrying with clear_exist=True
    when app keeps auto-formatting downgrades to warning & clears completion block;
    (4) numeric thousands commas are ignored when comparing numeric values."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from artemis.agents.flash.runner import FlashRunner, _TurnRecord
    from artemis.mcp.action_executor import McpActionExecutor
    from artemis.mcp.action_types import ActionResult
    from artemis.utils.visualization import format_minimal_list_with_elements
    from artemis.workflow.targets import check_field_value, normalize_field_value

    # (4) Numeric thousands comma normalization
    assert normalize_field_value("1,000,000") == normalize_field_value("1000000")
    assert check_field_value({"text": "1,000,000"}, "1000000") == (True, True, "1,000,000")

    pkg = "com.example.form"

    def make_tree(amount="", phone="", note="", prefill=""):
        return [
            {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
            {"bounds": "[50,100][950,200]", "text": amount, "hint": "Amount", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/amount", "package": pkg, "visible": True, "editable": True},
            {"bounds": "[50,250][950,350]", "text": phone, "hint": "Phone", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/phone", "package": pkg, "visible": True, "editable": True},
            {"bounds": "[50,400][950,500]", "text": note, "hint": "Note", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/note", "package": pkg, "visible": True, "editable": True},
            {"bounds": "[50,550][950,650]", "text": prefill, "hint": "HomePhone", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/home_phone", "package": pkg, "visible": True, "editable": True},
        ]

    live_tree = make_tree(prefill="+1")
    _, init_els, _ = format_minimal_list_with_elements(live_tree, 1000, 1000, compact=True)
    ctx = SimpleNamespace(
        last_raw_observation={"elements": live_tree, "width": 1000, "height": 1000, "seq": 1},
        data_engine=None,
        device=SimpleNamespace(device_width=1000, device_height=1000),
    )
    next_tree = {"tree": live_tree}

    class FakeSession:
        started = True

        async def call(self, name, args):
            return ActionResult.success(name, f"Typed '{args.get('text')}'")

        async def observe(self, settle_ms=0, include_image=False, persist=True):
            t = next_tree["tree"]
            xml, els, _ = format_minimal_list_with_elements(t, 1000, 1000, compact=True)
            ctx.last_raw_observation = {"elements": t, "width": 1000, "height": 1000, "seq": ctx.last_raw_observation["seq"] + 1}
            return SimpleNamespace(
                ok=True, hierarchy_ok=True, elements=els, elements_text=xml,
                width=1000, height=1000, screenshot_path=None, scrollable=False, message="ok", timed_out=False,
            )

    ex = McpActionExecutor(ctx, actuator=SimpleNamespace(extensions=lambda: (), controller=None), agent_name="flash")
    ex._session = FakeSession()
    state = SimpleNamespace(indexed_elements=init_els, indexed_points=[e["center"] for e in init_els], latest_screenshot=None, screen_scrollable=False)

    def idx_for(rid):
        return next(e["index"] for e in state.indexed_elements if e.get("resource_id") == f"{pkg}:id/{rid}")

    # 1) Numeric commas: "1000000" formatted as "1,000,000" -> passes cleanly
    next_tree["tree"] = make_tree(amount="1,000,000", prefill="+1")
    res_num = asyncio.run(ex.execute("input_text", {"target": idx_for("amount"), "text": "1000000"}, "tc-num", state))
    assert res_num.status == "success"
    assert "typed_value_mismatch" not in res_num.metadata

    # 2) App auto-formats "+84 912 345 678" when clear_exist=True retry happens (and first attempt):
    next_tree["tree"] = make_tree(amount="1,000,000", phone="+84 912 345 678", prefill="+1")
    res_vn1 = asyncio.run(ex.execute("input_text", {"target": idx_for("phone"), "text": "0912345678", "clear_exist": False}, "tc-vn1", state))
    assert res_vn1.status == "success"
    res_vn2 = asyncio.run(ex.execute("input_text", {"target": idx_for("phone"), "text": "0912345678", "clear_exist": True}, "tc-vn2", state))
    assert res_vn2.status == "success"
    assert ex.consume_unfixed_typed_mismatches() == []

    ex.task_goal = "Fill the form: amount 1000000, phone 0912345678, note Hello world"
    # 3) Intentional append ("Hello" + " world", clear_exist=False) -> no L-037 block & no mismatch!
    next_tree["tree"] = make_tree(amount="1,000,000", phone="+84 912 345 678", note="Hello", prefill="+1")
    res_h1 = asyncio.run(ex.execute("input_text", {"target": idx_for("note"), "text": "Hello"}, "tc-h1", state))
    assert res_h1.status == "success"
    next_tree["tree"] = make_tree(amount="1,000,000", phone="+84 912 345 678", note="Hello world", prefill="+1")
    res_h2 = asyncio.run(ex.execute("input_text", {"target": idx_for("note"), "text": " world", "clear_exist": False}, "tc-h2", state))
    assert res_h2.status == "success"
    assert "typed_value_mismatch" not in res_h2.metadata
    assert "Note: field holds" not in res_h2.text_summary

    # 3b) L-056: appending text the goal does NOT ask for onto a value typed in this run
    # ("Son" + "Dao" pattern) is still refused by L-037, even with clear_exist=False.
    res_bad = asyncio.run(ex.execute("input_text", {"target": idx_for("note"), "text": "Dao", "clear_exist": False}, "tc-bad", state))
    assert res_bad.status == "error"
    assert "already holds" in res_bad.text_summary

    # 4) Leftover prefill "+1" NOT typed in this run -> still FAILS!
    next_tree["tree"] = make_tree(amount="1,000,000", phone="+84 912 345 678", note="Hello world", prefill="+10913400134")
    res_pre = asyncio.run(ex.execute("input_text", {"target": idx_for("home_phone"), "text": "0913400134", "clear_exist": False}, "tc-pre", state))
    assert res_pre.status == "error"
    assert "clear_exist=true" in res_pre.text_summary
    assert len(ex.consume_unfixed_typed_mismatches()) == 1

    # Retrying home_phone after clearing "+1" succeeds (and even if app re-inserts +84 913 400 134 it warns only):
    next_tree["tree"] = make_tree(amount="1,000,000", phone="+84 912 345 678", note="Hello world", prefill="+84 913 400 134")
    res_retry = asyncio.run(ex.execute("input_text", {"target": idx_for("home_phone"), "text": "0913400134", "clear_exist": True}, "tc-retry", state))
    assert res_retry.status == "success"

    runner = FlashRunner.__new__(FlashRunner)
    runner.ctx = ctx
    runner.executor = ex
    runner.controller = MagicMock()
    runner.goal = "submit form"
    runner.fast_burst = True
    runner.summarizer = None
    runner._raise_if_device_offline = lambda **_kw: None
    runner._wf = None
    runner._loop_guard = None
    runner._scroll_probes = 99
    runner._finalize_task_report = AsyncMock(return_value={"status": "completed", "explanation": "ok"})
    msgs: list = []
    report, _, _, _ = asyncio.run(
        runner._process_tool_calls(
            [{"name": "report_task_status", "args": {"status": "completed", "explanation": "ok"}, "id": "tc-done"}],
            state, msgs, "done", {}, b"img", "", 0, _TurnRecord(),
        )
    )
    assert report == {"status": "completed", "explanation": "ok"}


def test_l053_compact_list_folds_keyboard_keys_keeps_action_keys_and_orders_app_first():
    """L-053: when an IME window is open, compact list prints app elements first,
    folds regular keyboard keys into one summary line, keeps action keys ('Done',
    'Hide keyboard') with valid indices, and ignores keyboard OCR during merge."""
    from artemis.utils.visualization import (
        _keyboard_window_info,
        format_minimal_list_with_elements,
        merge_ocr_into_list,
    )

    ime = "com.google.android.inputmethod.latin"
    app = "com.example.contacts"
    # Keyboard nodes deliberately appear FIRST (matching real Android dump order):
    kb_nodes = [
        {"bounds": "[0,1450][1080,2400]", "text": "", "class": "android.widget.FrameLayout", "package": ime, "visible": True},
        {"bounds": "[30,1470][160,1550]", "content-desc": "Hide keyboard", "class": "android.widget.ImageButton", "resource-id": f"{ime}:id/hide_keyboard", "package": ime, "visible": True, "clickable": True},
        {"bounds": "[200,1470][320,1550]", "text": "GIF", "class": "android.widget.Button", "package": ime, "visible": True, "clickable": True},
    ] + [
        {"bounds": f"[{20 + i * 105},1600][{115 + i * 105},1740]", "text": ch, "class": "android.view.View", "package": ime, "visible": True, "clickable": True}
        for i, ch in enumerate("qwertyuiop")
    ] + [
        {"bounds": "[880,2100][1050,2260]", "content-desc": "Done", "class": "android.widget.ImageButton", "resource-id": f"{ime}:id/key_pos_ime_action", "package": ime, "visible": True, "clickable": True},
    ]
    app_nodes = [
        {"bounds": "[0,0][1080,2400]", "text": "", "class": "android.widget.FrameLayout", "package": app, "visible": True},
        {"bounds": "[40,120][480,190]", "text": "Create contact", "class": "android.widget.TextView", "package": app, "visible": True},
        {"bounds": "[820,120][1020,190]", "text": "Save", "class": "android.widget.Button", "package": app, "visible": True, "clickable": True},
        {"bounds": "[100,400][980,500]", "text": "", "class": "android.widget.EditText", "package": app, "visible": True, "clickable": True, "editable": True, "focused": True},
    ]
    nodes = kb_nodes + app_nodes
    text, els, _ = format_minimal_list_with_elements(nodes, 1080, 2400, compact=True)
    lines = text.splitlines()

    # App elements appear BEFORE the keyboard summary line and regular keys ("q", "GIF") are folded:
    kb_summary_pos = next(i for i, l in enumerate(lines) if l.startswith("(keyboard open "))
    create_pos = next(i for i, l in enumerate(lines) if '"Create contact"' in l)
    input_pos = next(i for i, l in enumerate(lines) if "(input field)" in l)
    hide_pos = next(i for i, l in enumerate(lines) if '"Hide keyboard"' in l)
    done_pos = next(i for i, l in enumerate(lines) if '"Done"' in l)
    assert create_pos < input_pos < kb_summary_pos < hide_pos < done_pos
    assert not any('"q"' in l or '"GIF"' in l for l in lines)

    # Indices stay strictly aligned with elements[idx - 1], and kept action keys have is_keyboard=True:
    done_el = els[next(i for i, e in enumerate(els) if e.get("text") == "Done")]
    assert done_el["is_keyboard"] is True
    assert f"[{done_el['index']}] \"Done\"" in lines[done_pos]
    for pos_i, el in enumerate(els):
        assert el["index"] == pos_i + 1

    # OCR merge with ignore_boxes=[kb_box] skips keyboard OCR ("GIF") while adding app OCR ("Email"):
    _, kb_box = _keyboard_window_info(nodes, 1080, 2400)
    assert kb_box is not None
    ocr_reads = [
        {"text": "Email", "score": 0.99, "position": [{"x": 120, "y": 355}, {"x": 240, "y": 355}, {"x": 240, "y": 390}, {"x": 120, "y": 390}]},
        {"text": "English", "score": 0.99, "position": [{"x": 420, "y": 2150}, {"x": 620, "y": 2150}, {"x": 620, "y": 2200}, {"x": 420, "y": 2200}]},
    ]
    merged, _ = merge_ocr_into_list(text, els, ocr_reads, 1080, 2400, ignore_boxes=[kb_box])
    assert '(input field: "Email")' in merged
    assert "English" not in merged


def test_l054_selective_som_draws_only_unlabeled_inputs_and_ocr_within_budget():
    """L-054: draw_som_marks renders Set-of-Mark v2 boxes ONLY on unlabeled controls,
    input fields, and OCR gap-fill elements (never XML-labeled non-inputs or keyboard keys),
    wires cleanly through FlashRunner._build_tail with model_image.marks, and runs < 50ms."""
    from io import BytesIO
    import time
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from PIL import Image, ImageChops

    from artemis.config.agent import FlashModelImageConfig
    from artemis.agents.flash.runner import FlashRunner
    from artemis.utils.visualization import (
        draw_som_marks,
        format_minimal_list_with_elements,
        merge_ocr_into_list,
        select_som_elements,
    )

    ime = "com.google.android.inputmethod.latin"
    app = "com.example.contacts"
    nodes = [
        {"bounds": "[0,0][1080,2400]", "text": "", "class": "android.widget.FrameLayout", "package": app, "visible": True},
        {"bounds": "[40,100][400,180]", "text": "Contacts", "class": "android.widget.TextView", "package": app, "visible": True},
        {"bounds": "[450,240][630,420]", "text": "", "class": "android.widget.ImageButton", "package": app, "visible": True, "clickable": True},
        {"bounds": "[100,520][980,640]", "text": "", "class": "android.widget.EditText", "package": app, "visible": True, "clickable": True, "editable": True},
        {"bounds": "[0,1500][1080,2400]", "text": "", "class": "android.widget.FrameLayout", "package": ime, "visible": True},
        {"bounds": "[20,1600][120,1720]", "text": "a", "class": "android.view.View", "package": ime, "visible": True, "clickable": True},
        {"bounds": "[880,2100][1050,2260]", "content-desc": "Done", "class": "android.widget.ImageButton", "resource-id": f"{ime}:id/key_pos_ime_action", "package": ime, "visible": True, "clickable": True},
    ]
    txt, els, _ = format_minimal_list_with_elements(nodes, 1080, 2400, compact=True)
    ocr_reads = [{"text": "Email", "score": 0.99, "position": [{"x": 120, "y": 820}, {"x": 260, "y": 820}, {"x": 260, "y": 870}, {"x": 120, "y": 870}]}]
    merge_ocr_into_list(txt, els, ocr_reads, 1080, 2400)

    selected = select_som_elements(els)
    selected_indices = {e["index"] for e in selected}
    contacts_el = next(e for e in els if e.get("text") == "Contacts")
    avatar_el = next(e for e in els if e.get("is_icon") and "Image" in str(e.get("class") or ""))
    input_el = next(e for e in els if "EditText" in str(e.get("class") or ""))
    ocr_el = next(e for e in els if e.get("is_ocr"))
    done_el = next(e for e in els if e.get("text") == "Done")

    assert contacts_el["index"] not in selected_indices
    assert done_el["index"] not in selected_indices
    assert {avatar_el["index"], input_el["index"], ocr_el["index"]}.issubset(selected_indices)

    raw_img = Image.new("RGB", (1080, 2400), (245, 245, 245))
    buf = BytesIO()
    raw_img.save(buf, format="JPEG", quality=85)
    raw_bytes = buf.getvalue()

    marked_full = draw_som_marks(raw_bytes, els, max_side=0, jpeg_quality=85)
    marked_im = Image.open(BytesIO(marked_full))
    diff = ImageChops.difference(Image.open(BytesIO(raw_bytes)), marked_im)
    # Labeled "Contacts" & keyboard "Done" crops have zero annotation pixels; avatar/input have red marks:
    assert diff.crop((40, 100, 400, 180)).getbbox() is None
    assert diff.crop((880, 2100, 1050, 2260)).getbbox() is None
    assert diff.crop((445, 210, 635, 425)).getbbox() is not None
    assert diff.crop((95, 490, 985, 645)).getbbox() is not None

    # Timing check: warm draw + resize to max_side=768 under 50ms:
    _ = draw_som_marks(raw_bytes, els, max_side=768, jpeg_quality=75)
    t0 = time.monotonic()
    for _ in range(5):
        out_shrunk = draw_som_marks(raw_bytes, els, max_side=768, jpeg_quality=75)
    avg_ms = ((time.monotonic() - t0) / 5.0) * 1000.0
    assert avg_ms < 50.0, f"SoM drawing too slow: {avg_ms:.1f}ms/screen"
    assert Image.open(BytesIO(out_shrunk)).size == (346, 768)

    # FlashRunner._build_tail wiring for marks='unlabeled' vs 'off':
    runner = FlashRunner.__new__(FlashRunner)
    runner.image_on_demand = False
    ledger = MagicMock()
    ledger.elapsed_label.return_value = "T+00:05"

    runner.model_image_cfg = FlashModelImageConfig(max_side=768, jpeg_quality=75, marks="unlabeled")
    tail_on = runner._build_tail(ledger, 1, raw_bytes, txt, elements=els)
    img_block_on = next(b for b in tail_on.content if isinstance(b, dict) and b.get("type") == "image_url")

    runner.model_image_cfg = FlashModelImageConfig(max_side=768, jpeg_quality=75, marks="off")
    tail_off = runner._build_tail(ledger, 1, raw_bytes, txt, elements=els)
    img_block_off = next(b for b in tail_off.content if isinstance(b, dict) and b.get("type") == "image_url")
    assert img_block_on["image_url"]["url"] != img_block_off["image_url"]["url"]


def test_l057_recorder_skips_incomplete_or_wrong_field_runs_and_handles_passwords():
    """L-057: recorder refuses to save/merge workflows when a requested numeric/quoted/slot
    value is missing from both typing and the post-save outcome (Toan/Le without phone),
    when outcome screen echoes saved values yet drops one field, or when a slot lands in
    a field whose label conflicts with a goal keyword ('last name Le' typed into 'Company');
    password inputs and non-echoing confirmation screens still record."""
    from artemis.workflow.recorder import RawScreen, RecordedStep, record_run

    pkg = "com.example.contacts"
    R = lambda nodes: RawScreen(nodes, 1000, 1000)  # noqa: E731

    def form(name="", company="", first="", last="", phone="", *, two_names=False):
        nodes = [
            {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
            {"bounds": "[760,40][960,110]", "text": "Save", "class": "android.widget.Button", "resource-id": f"{pkg}:id/save", "package": pkg, "visible": True, "clickable": True},
            {"bounds": "[80,520][920,600]", "text": company, "hint": "Company", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/company", "package": pkg, "visible": True, "editable": True},
            {"bounds": "[80,640][920,720]", "text": phone, "hint": "Phone", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/phone", "package": pkg, "visible": True, "editable": True},
        ]
        if two_names:
            nodes.insert(2, {"bounds": "[80,260][920,340]", "text": first, "hint": "First name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/first", "package": pkg, "visible": True, "editable": True})
            nodes.insert(3, {"bounds": "[80,380][920,460]", "text": last, "hint": "Last name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/last", "package": pkg, "visible": True, "editable": True})
        else:
            nodes.insert(2, {"bounds": "[80,260][920,340]", "text": name, "hint": "Name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/name", "package": pkg, "visible": True, "editable": True})
        return nodes

    def detail(*labels):
        return [
            {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
            *[
                {"bounds": f"[60,{160 + i * 90}][800,{230 + i * 90}]", "text": lbl, "class": "android.widget.TextView", "package": pkg, "visible": True}
                for i, lbl in enumerate(labels)
            ],
        ]

    # 1) Toan / Le bug: Qwen typed "Toan" -> Name, "Le" -> Company, skipped phone "0916200162", pressed Save:
    bad_toan = [
        RecordedStep(tool="input_text", args={"text": "Toan", "target": 3}, action={"normalized_coordinates": [300, 300]}, pre=R(form()), post=R(form(name="Toan"))),
        RecordedStep(tool="input_text", args={"text": "Le", "target": 4}, action={"normalized_coordinates": [300, 560]}, pre=R(form(name="Toan")), post=R(form(name="Toan", company="Le"))),
        RecordedStep(tool="click", args={"target": 2}, action={"normalized_coordinates": [850, 75], "target_text": "Save", "target_resource_id": f"{pkg}:id/save"}, pre=R(form(name="Toan", company="Le")), post=R(detail("Toan", "Le"))),
    ]
    assert record_run(bad_toan, goal="Create contact Toan Le, phone 0916200162") is None

    # 2) Typed all 3, but "Le" landed in Company instead of Last name when goal explicitly said "last name Le":
    wrong_field = [
        RecordedStep(tool="input_text", args={"text": "Toan", "target": 3}, action={"normalized_coordinates": [300, 300]}, pre=R(form(two_names=True)), post=R(form(first="Toan", two_names=True))),
        RecordedStep(tool="input_text", args={"text": "Le", "target": 5}, action={"normalized_coordinates": [300, 560]}, pre=R(form(first="Toan", two_names=True)), post=R(form(first="Toan", company="Le", two_names=True))),
        RecordedStep(tool="input_text", args={"text": "0916200162", "target": 6}, action={"normalized_coordinates": [300, 680]}, pre=R(form(first="Toan", company="Le", two_names=True)), post=R(form(first="Toan", company="Le", phone="0916200162", two_names=True))),
        RecordedStep(tool="click", args={"target": 2}, action={"normalized_coordinates": [850, 75], "target_text": "Save", "target_resource_id": f"{pkg}:id/save"}, pre=R(form(first="Toan", company="Le", phone="0916200162", two_names=True)), post=R(detail("Toan", "Le", "0916200162"))),
    ]
    assert record_run(wrong_field, goal="Create contact first name Toan last name Le phone 0916200162") is None

    # 3) Good run (Name="Toan Le", Phone="0916200162", detail shows formatted "+84 916 200 162") records cleanly:
    good_run = [
        RecordedStep(tool="input_text", args={"text": "Toan Le", "target": 3}, action={"normalized_coordinates": [300, 300]}, pre=R(form()), post=R(form(name="Toan Le"))),
        RecordedStep(tool="input_text", args={"text": "0916200162", "target": 5}, action={"normalized_coordinates": [300, 680]}, pre=R(form(name="Toan Le")), post=R(form(name="Toan Le", phone="0916200162"))),
        RecordedStep(tool="click", args={"target": 2}, action={"normalized_coordinates": [850, 75], "target_text": "Save", "target_resource_id": f"{pkg}:id/save"}, pre=R(form(name="Toan Le", phone="0916200162")), post=R(detail("Toan Le", "+84 916 200 162"))),
    ]
    assert record_run(good_run, goal="Create contact Toan Le, phone 0916200162") is not None


def test_l058_and_l059_completion_check_and_typed_progress_tracker():
    """L-058 & L-059: (1) _typed_progress_line tracks (text -> field label) per typed field and
    shows requested literals status (not added on non-form tasks); (2) report_task_status(completed)
    rejects once when a requested literal was never typed and never shown on screen, then accepts
    when typed or after retry/explanation."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from artemis.agents.flash.runner import FlashRunner, _TurnRecord
    from artemis.mcp.action_executor import McpActionExecutor
    from artemis.mcp.action_types import ActionResult
    from artemis.utils.visualization import format_minimal_list_with_elements

    pkg = "com.example.contacts"

    def form_tree(name="", company="", phone=""):
        return [
            {"bounds": "[0,0][1000,1000]", "class": "android.widget.FrameLayout", "package": pkg, "visible": True},
            {"bounds": "[80,200][920,280]", "text": name, "hint": "Name", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/name", "package": pkg, "visible": True, "editable": True},
            {"bounds": "[80,320][920,400]", "text": company, "hint": "Company", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/company", "package": pkg, "visible": True, "editable": True},
            {"bounds": "[80,440][920,520]", "text": phone, "hint": "Phone", "class": "android.widget.EditText", "resource-id": f"{pkg}:id/phone", "package": pkg, "visible": True, "editable": True},
        ]

    init_tree = form_tree()
    init_xml, init_els, _ = format_minimal_list_with_elements(init_tree, 1000, 1000, compact=True)
    ctx = SimpleNamespace(
        last_raw_observation={"elements": init_tree, "width": 1000, "height": 1000, "seq": 1},
        data_engine=None,
        device=SimpleNamespace(device_width=1000, device_height=1000),
    )

    ex = McpActionExecutor(ctx, actuator=SimpleNamespace(extensions=lambda: (), controller=None), agent_name="flash")
    runner = FlashRunner.__new__(FlashRunner)
    runner.ctx = ctx
    runner.executor = ex
    runner.controller = MagicMock()
    runner.goal = "Create contact Toan Le, phone 0916200162"
    runner.fast_burst = True
    runner.summarizer = None
    runner._raise_if_device_offline = lambda **_kw: None
    runner._wf = None
    runner._loop_guard = None
    runner._scroll_probes = 99
    runner._typed_field_history = []
    runner._reminded_missing_literals = False
    runner.image_on_demand = False
    runner._finalize_task_report = AsyncMock(return_value={"status": "completed", "explanation": "done"})

    state = SimpleNamespace(indexed_elements=init_els, indexed_points=[e["center"] for e in init_els], latest_screenshot=None, screen_scrollable=False)

    # Record wrong placement ("Toan" -> Name, "Le" -> Company):
    ctx.last_raw_observation = {"elements": form_tree(name="Toan"), "width": 1000, "height": 1000, "seq": 2}
    runner._record_typed_field_entry(
        {"target": 1, "text": "Toan"},
        index_elements=init_els,
        state=state,
        pre_raw={"elements": init_tree, "width": 1000, "height": 1000},
        exec_result=SimpleNamespace(status="success", metadata={"target_coordinates": init_els[0]["center"]}),
    )
    ctx.last_raw_observation = {"elements": form_tree(name="Toan", company="Le"), "width": 1000, "height": 1000, "seq": 3}
    runner._record_typed_field_entry(
        {"target": 2, "text": "Le"},
        index_elements=init_els,
        state=state,
        pre_raw={"elements": form_tree(name="Toan"), "width": 1000, "height": 1000},
        exec_result=SimpleNamespace(status="success", metadata={"target_coordinates": init_els[1]["center"]}),
    )

    ledger = MagicMock()
    ledger.elapsed_label.return_value = "T+00:10"
    tail = runner._build_tail(ledger, 2, None, init_xml, elements=init_els)
    header_txt = tail.content[0]["text"]
    assert "'Toan' → Name" in header_txt and "'Le' → Company" in header_txt
    assert '"0916200162" ✗ not entered yet' in header_txt

    # Non-form task (no literals & nothing typed) produces NO task-values line:
    plain_runner = FlashRunner.__new__(FlashRunner)
    plain_runner.goal = "Open Settings and toggle Wi-Fi"
    plain_runner.ctx = SimpleNamespace(last_raw_observation=None)
    plain_runner.image_on_demand = False
    plain_tail = plain_runner._build_tail(ledger, 1, None, "[1] \"Wi-Fi\" 0,0,100,100")
    assert "Typed so far" not in plain_tail.content[0]["text"]
    assert "Requested values" not in plain_tail.content[0]["text"]

    # L-058: report_task_status(completed) without typing/showing "0916200162" reminds once:
    msgs1: list = []
    rep1, _, _, _ = asyncio.run(
        runner._process_tool_calls(
            [{"name": "report_task_status", "args": {"status": "completed", "explanation": "created"}, "id": "tc-m1"}],
            state, msgs1, "done", {}, b"img", init_xml, 0, _TurnRecord(),
        )
    )
    assert rep1 is None and msgs1[0].status == "error"
    assert "0916200162" in msgs1[0].content

    # Typing "0916200162" satisfies L-058 and updates L-059 status:
    ctx.last_raw_observation = {"elements": form_tree(name="Toan", company="Le", phone="0916200162"), "width": 1000, "height": 1000, "seq": 4}
    runner._record_typed_field_entry(
        {"target": 3, "text": "0916200162"},
        index_elements=init_els,
        state=state,
        pre_raw={"elements": form_tree(name="Toan", company="Le"), "width": 1000, "height": 1000},
        exec_result=SimpleNamespace(status="success", metadata={"target_coordinates": init_els[2]["center"]}),
    )
    runner._reminded_missing_literals = False  # verify clean pass even before consuming reminder
    tail2 = runner._build_tail(ledger, 3, None, init_xml, elements=init_els)
    assert '"0916200162" ✓ typed (Phone)' in tail2.content[0]["text"]

    msgs2: list = []
    rep2, _, _, _ = asyncio.run(
        runner._process_tool_calls(
            [{"name": "report_task_status", "args": {"status": "completed", "explanation": "created"}, "id": "tc-m2"}],
            state, msgs2, "done", {}, b"img", init_xml, 0, _TurnRecord(),
        )
    )
    assert rep2 == {"status": "completed", "explanation": "done"}


def test_l060_swipe_diff_direction_and_scroll_edge_feedback():
    """L-060: swipe reports finger vs page direction plus New/Hidden elements,
    records TOP/BOTTOM edge hints ONLY after a blocked directional swipe,
    exposes the edge hint in _build_tail, and resets it once the page moves."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from artemis.agents.flash.runner import FlashRunner
    from artemis.mcp.action_executor import McpActionExecutor
    from artemis.mcp.action_types import ActionResult
    from artemis.utils.visualization import format_minimal_list_with_elements

    pkg = "com.example.contacts"

    def make_tree(items, *, scrollable=True):
        return [
            {"bounds": "[0,0][1000,1000]", "class": "android.widget.ScrollView", "package": pkg, "visible": True, "scrollable": scrollable},
            *items,
        ]

    top_items = [
        {"bounds": "[420,120][580,260]", "text": "", "class": "android.widget.ImageButton", "package": pkg, "visible": True, "clickable": True},
        {"bounds": "[360,270][640,320]", "text": "Choose picture", "class": "android.widget.TextView", "package": pkg, "visible": True},
        {"bounds": "[80,400][920,480]", "text": "", "hint": "Name", "class": "android.widget.EditText", "package": pkg, "visible": True, "editable": True},
    ]
    mid_items = [
        {"bounds": "[80,200][920,280]", "text": "", "hint": "Name", "class": "android.widget.EditText", "package": pkg, "visible": True, "editable": True},
        {"bounds": "[80,400][920,480]", "text": "", "hint": "Phone", "class": "android.widget.EditText", "package": pkg, "visible": True, "editable": True},
        {"bounds": "[80,550][920,630]", "text": "", "hint": "Email", "class": "android.widget.EditText", "package": pkg, "visible": True, "editable": True},
    ]

    mid_tree = make_tree(mid_items)
    mid_xml, mid_els, _ = format_minimal_list_with_elements(mid_tree, 1000, 1000, compact=True)
    top_tree = make_tree(top_items)
    top_xml, top_els, _ = format_minimal_list_with_elements(top_tree, 1000, 1000, compact=True)

    ctx = SimpleNamespace(
        last_raw_observation={"elements": mid_tree, "width": 1000, "height": 1000, "seq": 1},
        data_engine=None,
        device=SimpleNamespace(device_width=1000, device_height=1000),
    )
    obs_state = {"tree": top_tree, "els": top_els, "xml": top_xml, "scrollable": True}

    class FakeSession:
        started = True

        async def call(self, name, args):
            return ActionResult.success(name, "Swiped.")

        async def observe(self, settle_ms=0, include_image=False, persist=True):
            ctx.last_raw_observation = {"elements": obs_state["tree"], "width": 1000, "height": 1000, "seq": 2}
            return SimpleNamespace(
                ok=True, hierarchy_ok=True, elements=obs_state["els"], elements_text=obs_state["xml"],
                width=1000, height=1000, screenshot_path=None, scrollable=obs_state["scrollable"], message="ok", timed_out=False,
            )

    ex = McpActionExecutor(ctx, actuator=SimpleNamespace(extensions=lambda: (), controller=None), agent_name="flash")
    ex._session = FakeSession()
    ex._wait_screen_settled = lambda *_a, **_kw: asyncio.sleep(0)
    state = SimpleNamespace(indexed_elements=mid_els, indexed_points=[e["center"] for e in mid_els], latest_screenshot=None, screen_scrollable=True)

    # 1) Initial swipe down moves toward TOP and reports New & Hidden elements:
    res_down = asyncio.run(ex.execute("swipe", {"direction": "down"}, "tc-s1", state, index_elements=mid_els))
    assert res_down.status == "success"
    assert "moved toward its TOP" in res_down.text_summary
    assert 'New: "Choose picture", (icon)' in res_down.text_summary
    assert 'Hidden: "Phone", "Email"' in res_down.text_summary
    assert ex.scroll_edge_state() is None

    # 2) Swiping down again at TOP edge produces No visible change -> TOP edge hint:
    res_top = asyncio.run(ex.execute("swipe", {"direction": "down"}, "tc-s2", state, index_elements=top_els))
    assert "already at the TOP edge" in res_top.text_summary
    assert "direction='up'" in res_top.text_summary
    assert ex.scroll_edge_state() == "top"

    runner = FlashRunner.__new__(FlashRunner)
    runner.executor = ex
    runner.goal = "Open Settings"
    runner.ctx = ctx
    runner.image_on_demand = False
    ledger = MagicMock()
    ledger.elapsed_label.return_value = "T+00:12"
    tail_top = runner._build_tail(ledger, 2, None, top_xml, scrollable=True)
    assert "hit TOP edge" in tail_top.content[0]["text"]
    assert "direction='up'" in tail_top.content[0]["text"]

    # 3) Swiping up moves back toward BOTTOM and clears TOP edge state:
    obs_state.update({"tree": mid_tree, "els": mid_els, "xml": mid_xml})
    res_up = asyncio.run(ex.execute("swipe", {"direction": "up"}, "tc-s3", state, index_elements=top_els))
    assert "moved toward its BOTTOM" in res_up.text_summary
    assert ex.scroll_edge_state() is None
    tail_mid = runner._build_tail(ledger, 3, None, mid_xml, scrollable=True)
    assert "[Scroll: scrollable container detected]" in tail_mid.content[0]["text"]


def test_l061_quoted_ui_labels_are_not_required_literals():
    """L-061: quoted strings in a goal are often UI labels or forbidden controls
    ("do not tap 'Place Order'"); they must never become values the completion
    check (L-058) or the recorder (L-057) demand on the final screen."""
    from artemis.workflow.targets import extract_requested_literals

    goal = (
        'On Grab: empty the basket, confirm "Yes"/"Remove" if asked, never tap "Place Order" '
        "or 'Đặt đơn'; then call 0916400164"
    )
    assert extract_requested_literals(goal) == ["0916400164"]
    assert "Place Order" in extract_requested_literals(goal, include_quoted=True)
