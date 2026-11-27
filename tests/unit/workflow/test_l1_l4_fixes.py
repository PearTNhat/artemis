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
    from artemis.workflow.targets import TargetDescriptor, resolve

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
    from artemis.workflow.targets import TargetDescriptor

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
