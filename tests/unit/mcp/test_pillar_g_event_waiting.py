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

"""Unit tests for Pillar G: Event-driven waiting, cross-screen targeting, and Anchor Guard."""

import asyncio
import base64
import io
from unittest.mock import AsyncMock, Mock
from PIL import Image, ImageDraw
import pytest

from artemis.mcp.action_executor import McpActionExecutor, _ArgError
from artemis.mcp.action_types import ActionCode
from artemis.mcp.actuators.adb import AdbActuator
from artemis.utils.screen_diff import (
    compare_screen_pixels,
    compare_xml_hierarchy,
    find_matching_text_elements,
    flatten_hierarchy,
    mask_noise,
    unaccent,
)


def _make_executor(width=1080, height=2400):
    ctx = Mock()
    ctx.device.device_width = width
    ctx.device.device_height = height
    actuator = Mock()
    actuator.controller = Mock()
    actuator.extensions.return_value = []
    return McpActionExecutor(ctx, actuator=actuator)


def _indexed_state():
    state = Mock()
    state.indexed_elements = [
        {
            "index": 1,
            "center": [540, 400],
            "text": "Order summary",
            "bounds": [100, 350, 980, 450],
            "class": "android.widget.TextView",
        },
        {
            "index": 2,
            "center": [540, 1200],
            "text": "Review address",
            "bounds": [100, 1150, 980, 1250],
            "class": "android.widget.Button",
        },
        {
            "index": 3,
            "center": [540, 2000],
            "text": "Place order",
            "bounds": [100, 1950, 980, 2050],
            "class": "android.widget.Button",
        },
    ]
    state.indexed_points = [[540, 400], [540, 1200], [540, 2000]]
    state.latest_ui_hierarchy = None
    return state


# --- Target text & Anchor Guard tests ------------------------------------------------


def test_target_text_resolves_to_element_center():
    executor = _make_executor(width=1000, height=2000)
    state = _indexed_state()
    # click with target_text="Review address"
    wire_name, wire_args, _, recorded = executor._translate(
        "click", {"target_text": "Review address"}, state
    )
    assert wire_name == "click"
    # center [540, 1200] in 1000x2000 => [540, 600] in 0-1000 normalized
    assert wire_args["target"] == [540, 600]
    assert recorded["target_text"] == "Review address"
    assert recorded["snapped_to_text"] == "Review address"


def test_target_text_anchor_guard_fails_when_not_found():
    executor = _make_executor()
    state = _indexed_state()
    with pytest.raises(_ArgError) as excinfo:
        executor._translate("click", {"target_text": "Non-existent text"}, state)
    assert "Anchor Guard: target_text 'Non-existent text' for click was not found on screen" in str(excinfo.value)


def test_target_text_commitment_guard_aborts_sensitive_action():
    executor = _make_executor()
    state = _indexed_state()
    with pytest.raises(_ArgError) as excinfo:
        executor._translate("click", {"target_text": "Place order"}, state)
    assert "Commitment Guard" in str(excinfo.value)


def test_target_text_ambiguity_guard_aborts_when_multiple_matches():
    executor = _make_executor()
    state = Mock()
    state.indexed_elements = [
        {"index": 1, "center": [100, 200], "text": "Select", "bounds": [50, 150, 150, 250]},
        {"index": 2, "center": [100, 600], "text": "Select", "bounds": [50, 550, 150, 650]},
    ]
    with pytest.raises(_ArgError) as excinfo:
        executor._translate("click", {"target_text": "Select"}, state)
    assert "ambiguous" in str(excinfo.value)


def test_unaccented_matching():
    assert unaccent("Đặt hàng") == "dat hang"
    assert unaccent("Tài khoản") == "tai khoan"
    assert unaccent("Đang tải...") == "dang tai..."

    elements = [
        {"text": "Đặt hàng", "bounds": [100, 100, 400, 200], "center": [250, 150]}
    ]
    matches = find_matching_text_elements(elements, "dat hang")
    assert len(matches) == 1
    assert matches[0]["text"] == "Đặt hàng"


# --- Event-driven wait tool tests ----------------------------------------------------


def test_wait_tools_translation():
    executor = _make_executor()
    state = _indexed_state()

    # wait_for_screen_change
    wire_name, wire_args, _, _ = executor._translate(
        "wait_for_screen_change", {"timeout_s": 20, "region": [100, 200, 800, 900]}, state
    )
    assert wire_name == "wait_for_screen_change"
    assert wire_args["timeout_ms"] == 20000
    assert wire_args["region"] == [100, 200, 800, 900]

    # wait_for_text
    wire_name, wire_args, _, _ = executor._translate(
        "wait_for_text", {"text": ["Ready", "Sẵn sàng"], "timeout_s": 10}, state
    )
    assert wire_name == "wait_for_text"
    assert wire_args["text"] == ["Ready", "Sẵn sàng"]
    assert wire_args["timeout_ms"] == 10000
    assert wire_args["wait_state"] == "appear"

    # wait_until_gone
    wire_name, wire_args, _, _ = executor._translate(
        "wait_until_gone", {"text": "Loading...", "timeout_s": 12}, state
    )
    assert wire_name == "wait_for_text"
    assert wire_args["text"] == "Loading..."
    assert wire_args["timeout_ms"] == 12000
    assert wire_args["wait_state"] == "disappear"


@pytest.mark.asyncio
async def test_adb_actuator_wait_for_text_success_and_timeout():
    ctx = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    actuator = AdbActuator(ctx)

    # Mock screen data response
    mock_data = Mock()
    mock_data.width = 1080
    mock_data.height = 2400
    mock_data.elements = [
        {"text": "Order confirmed", "bounds": [100, 200, 500, 300], "center": [300, 250]}
    ]
    mock_data.base64 = None
    actuator.controller.get_screen_data = AsyncMock(return_value=mock_data)

    # 1. Successful appear
    res = await actuator.wait_for_text("Order confirmed", wait_state="appear", timeout_ms=500)
    assert res.ok is True
    assert "Found text 'Order confirmed'" in res.message

    # 2. Timeout appear (soft handover: returns ok=True with [TIMEOUT_REACHED] and counter)
    res = await actuator.wait_for_text("Missing header", wait_state="appear", timeout_ms=300)
    assert res.ok is True
    assert "[TIMEOUT_REACHED]" in res.message
    assert "chưa thấy xuất hiện text" in res.message
    assert "lần chờ thứ 1" in res.message

    # 3. Repeated timeout increments wait count
    res2 = await actuator.wait_for_text("Missing header", wait_state="appear", timeout_ms=300)
    assert res2.ok is True
    assert "lần chờ thứ 2" in res2.message

    # 4. Interactive action resets wait counter
    await actuator.click(500, 500)
    assert actuator._wait_count == 0
    assert actuator._cumulative_wait_s == 0.0


@pytest.mark.asyncio
async def test_adb_actuator_wait_for_screen_change_detected():
    ctx = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    actuator = AdbActuator(ctx)

    img1 = Image.new("RGB", (1080, 2400), color="white")
    buf1 = io.BytesIO()
    img1.save(buf1, format="JPEG")
    b64_1 = base64.b64encode(buf1.getvalue()).decode("utf-8")

    img2 = Image.new("RGB", (1080, 2400), color="blue")
    buf2 = io.BytesIO()
    img2.save(buf2, format="JPEG")
    b64_2 = base64.b64encode(buf2.getvalue()).decode("utf-8")

    data1 = Mock(base64=b64_1, width=1080, height=2400, elements=[])
    data2 = Mock(base64=b64_2, width=1080, height=2400, elements=[])

    # First returns data1 (stabilization), then data2 (change detected)
    actuator.controller.get_screen_data = AsyncMock(side_effect=[data1, data1, data2])

    res = await actuator.wait_for_screen_change(timeout_ms=3000)
    assert res.ok is True
    assert "Screen visually changed" in res.message or "Screen changed" in res.message


def test_crop_region_strict_x1_y1_x2_y2():
    from artemis.utils.screen_diff import crop_region

    img = Image.new("RGB", (1000, 1000), color="white")
    # AI specifies chessboard: x from 0 to 1000, y from 300 to 750 => [0, 300, 1000, 750]
    cropped = crop_region(img, [0, 300, 1000, 750], width=1000, height=1000)
    # Width must be 1000, height must be 450 (750 - 300)
    assert cropped.size == (1000, 450)


@pytest.mark.asyncio
async def test_wait_until_gone_empty_xml_does_not_declare_gone():
    ctx = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    actuator = AdbActuator(ctx)

    # 1. First poll: Spinner is present
    data_with_spinner = Mock(
        elements=[{"text": "Đang tìm tài xế...", "bounds": [100, 200, 500, 300]}],
        width=1080,
        height=2400,
    )
    # 2. Second poll: XML glitch (empty list) -> should NOT trigger gone!
    data_glitch_empty = Mock(elements=[], width=1080, height=2400)
    # 3. Third poll: Spinner is still present!
    # 4. Fourth & Fifth poll: Gone for real with normal content present
    data_normal_content = Mock(
        elements=[{"text": "Tài xế Nguyễn Văn A", "bounds": [100, 200, 500, 300]}],
        width=1080,
        height=2400,
    )

    actuator.controller.get_screen_data = AsyncMock(
        side_effect=[
            data_with_spinner,
            data_glitch_empty,
            data_with_spinner,
            data_normal_content,
            data_normal_content,
        ]
    )

    res = await actuator.wait_until_gone("Đang tìm tài xế", timeout_ms=5000)
    assert res.ok is True
    assert "disappeared from screen" in res.message


def test_compare_xml_hierarchy_with_region_filtering():
    from artemis.utils.screen_diff import compare_xml_hierarchy

    # Region: lower half of screen [0, 500, 1000, 1000]
    region = [0, 500, 1000, 1000]
    width = 1000
    height = 1000

    # Header changed at y=100 (outside region)
    el1 = [{"text": "Title 1", "bounds": [100, 50, 900, 150]}]
    el2 = [{"text": "Title 2", "bounds": [100, 50, 900, 150]}]

    changed, _ = compare_xml_hierarchy(el1, el2, region=region, width=width, height=height)
    assert changed is False, "Changes outside region must be ignored"

    # Board changed at y=700 (inside region)
    el3 = [{"text": "Knight e4", "bounds": [100, 650, 300, 750]}]
    el4 = [{"text": "Knight f6", "bounds": [100, 650, 300, 750]}]

    changed_inside, diff = compare_xml_hierarchy(el3, el4, region=region, width=width, height=height)
    assert changed_inside is True, "Changes inside region must be detected"
    assert "added: ['Knight f6']" in diff


def test_compare_screen_pixels_cell_threshold_default():
    from artemis.utils.screen_diff import compare_screen_pixels

    img1 = Image.new("RGB", (100, 100), color="black")
    img2 = Image.new("RGB", (100, 100), color="black")
    draw = ImageDraw.Draw(img2)
    # Draw a tiny dot (less than 20 cells when downscaled to 64x64)
    draw.rectangle([5, 5, 8, 8], fill="white")

    changed, cells = compare_screen_pixels(img1, img2)
    # Default cell_threshold=20 rejects tiny changes
    assert changed is False
    assert cells < 20


def test_status_bar_mask_height_4_percent():
    from artemis.utils.screen_diff import mask_noise

    img = Image.new("L", (1000, 2000), color=255)
    masked = mask_noise(img, elements=[], width=1000, height=2000)
    pixels = masked.load()

    # 4% of 2000 is 80px. Top 80px should be masked (0), while pixel at y=85 should still be 255
    assert pixels[500, 40] == 0  # inside status bar
    assert pixels[500, 90] == 255  # below status bar, app content preserved


def test_occlusion_guard_injects_flag_in_elements():
    from artemis.utils.visualization import format_minimal_list_with_elements

    # Overlapping elements: idx 1 and idx 2 overlap > 50%
    fused = [
        {
            "text": "List item",
            "bounds": "[100,1000][900,1200]",
            "class": "android.view.View",
        },
        {
            "text": "Floating button",
            "bounds": "[700,1050][900,1200]",
            "class": "android.widget.Button",
        },
    ]
    _, elements, _ = format_minimal_list_with_elements(fused, width=1000, height=2000)
    occluded_els = [el for el in elements if el.get("occluded")]
    assert len(occluded_els) > 0, "Occluded element must have el['occluded'] == True"

    # Now verify Anchor Guard in McpActionExecutor rejects occluded best match
    executor = _make_executor(width=1000, height=2000)
    state = Mock()
    state.indexed_elements = elements
    state.indexed_points = [el["center"] for el in elements]
    state.latest_ui_hierarchy = None

    with pytest.raises(_ArgError) as excinfo:
        executor._translate("click", {"target_text": occluded_els[0]["text"]}, state)
    assert "is occluded or obscured" in str(excinfo.value)


def test_is_interactive_boolean_string():
    from artemis.utils.visualization import format_minimal_list_with_elements

    # Node with string "false" for checked
    node = {
        "text": "Inactive option",
        "bounds": "[100,500][900,600]",
        "checked": "false",
        "clickable": "false",
        "focusable": "false",
        "class": "android.widget.TextView",
    }
    # Many items to trigger > 80 threshold
    fused = [
        {
            "text": f"Item {i}",
            "bounds": f"[100,{i*10}][900,{i*10+8}]",
            "clickable": "true",
            "class": "android.widget.TextView",
        }
        for i in range(85)
    ]
    fused.append(node)

    _, elements, _ = format_minimal_list_with_elements(fused, width=1000, height=2000)
    last_el = next((e for e in elements if e.get("text") == "Inactive option"), None)
    # Since checked="false" is not truthy, it is filtered out above 100 items if non-interactive
    assert last_el is not None
    assert last_el.get("checked") == "false"


def test_flatten_hierarchy_nested_structures():
    # 1. Empty / falsy
    assert flatten_hierarchy([]) == []
    assert flatten_hierarchy(None) == []

    # 2. Already flat list
    flat_input = [{"text": "A"}, {"text": "B"}]
    assert flatten_hierarchy(flat_input) == [{"text": "A"}, {"text": "B"}]

    # 3. Single root dict with children
    tree_input = {
        "text": "Root",
        "children": [
            {"text": "Child 1"},
            {"text": "Child 2", "children": [{"text": "Grandchild 1"}]},
        ],
    }
    flattened = flatten_hierarchy(tree_input)
    assert len(flattened) == 4
    assert [n["text"] for n in flattened] == ["Root", "Child 1", "Child 2", "Grandchild 1"]
    assert "children" not in flattened[0]
    assert "children" not in flattened[2]

    # 4. List of nested nodes
    list_trees = [
        {"text": "R1", "children": [{"text": "C1"}]},
        {"text": "R2", "children": [{"text": "C2"}]},
    ]
    flattened_list = flatten_hierarchy(list_trees)
    assert len(flattened_list) == 4
    assert [n["text"] for n in flattened_list] == ["R1", "C1", "R2", "C2"]


@pytest.mark.asyncio
async def test_target_text_ambiguity_guard_with_fresh_observe_data():
    executor = _make_executor(width=1000, height=2000)
    state = _indexed_state()

    # Fresh observation returns two elements matching "Confirm" at distant centers: [200, 500] and [800, 500]
    fresh_elements = [
        {
            "index": 1,
            "text": "Confirm",
            "center": [200, 500],
            "bounds": [100, 450, 300, 550],
            "class": "android.widget.Button",
        },
        {
            "index": 2,
            "text": "Confirm",
            "center": [800, 500],
            "bounds": [700, 450, 900, 550],
            "class": "android.widget.Button",
        },
    ]

    session_mock = Mock()
    session_mock.started = True
    session_mock.call = AsyncMock()
    session_mock.observe = AsyncMock()
    obs_mock = Mock()
    obs_mock.ok = True
    obs_mock.hierarchy_ok = True
    obs_mock.elements = fresh_elements
    obs_mock.screenshot_path = None
    obs_mock.elements_text = ""
    session_mock.observe.return_value = obs_mock

    executor._session = session_mock

    res = await executor.execute("click", {"target_text": "Confirm"}, "call_ambiguity", state)
    assert res.status == "error"
    assert "ambiguous" in res.text_summary
    assert "2 matches found at different positions" in res.text_summary


@pytest.mark.asyncio
async def test_target_text_updates_state_indexed_elements_with_processed_format():
    from artemis.mcp.action_types import ActionResult

    executor = _make_executor(width=1000, height=2000)
    state = _indexed_state()

    # Fresh observation returns properly processed elements with center, index, bounds
    fresh_elements = [
        {
            "index": 1,
            "text": "Search settings",
            "center": [500, 300],
            "bounds": [100, 250, 900, 350],
            "class": "android.widget.EditText",
        }
    ]

    session_mock = Mock()
    session_mock.started = True
    session_mock.call = AsyncMock(return_value=ActionResult.success("click", "Clicked"))
    session_mock.observe = AsyncMock()
    obs_mock = Mock()
    obs_mock.ok = True
    obs_mock.hierarchy_ok = True
    obs_mock.elements = fresh_elements
    obs_mock.screenshot_path = None
    obs_mock.elements_text = ""
    session_mock.observe.return_value = obs_mock

    executor._session = session_mock

    res = await executor.execute("click", {"target_text": "Search settings"}, "call_ok", state)
    assert res.status == "success"

    # state.indexed_elements was updated with processed elements from observe(), not raw nodes
    assert len(state.indexed_elements) == 1
    assert state.indexed_elements[0]["center"] == [500, 300]
    assert state.indexed_elements[0]["index"] == 1
    assert state.indexed_points == [[500, 300]]



def _live_session(elements, *, hierarchy_ok=True, ok=True):
    from artemis.mcp.action_types import ActionResult

    session_mock = Mock()
    session_mock.started = True
    session_mock.call = AsyncMock(return_value=ActionResult.success("click", "Clicked"))
    obs_mock = Mock()
    obs_mock.ok = ok
    obs_mock.hierarchy_ok = hierarchy_ok
    obs_mock.elements = elements
    obs_mock.screenshot_path = None
    obs_mock.elements_text = ""
    session_mock.observe = AsyncMock(return_value=obs_mock)
    return session_mock


@pytest.mark.asyncio
async def test_target_text_never_falls_back_to_the_stale_turn_snapshot(monkeypatch):
    """The text exists only on the screen the turn started on ("Review address"
    in the snapshot); the live screen no longer shows it. The tap must be refused
    rather than land on the old position on the new screen."""
    executor = _make_executor(width=1000, height=2000)
    state = _indexed_state()
    live_elements = [
        {
            "index": 1,
            "text": "Address details",
            "center": [500, 300],
            "bounds": [100, 250, 900, 350],
            "class": "android.widget.TextView",
        }
    ]
    session_mock = _live_session(live_elements)
    executor._session = session_mock
    monkeypatch.setattr("artemis.mcp.action_executor.asyncio.sleep", AsyncMock())

    res = await executor.execute("click", {"target_text": "Review address"}, "call_stale", state)

    assert res.status == "error"
    assert "was not found on screen" in res.text_summary
    session_mock.call.assert_not_called()
    # The state now mirrors the live screen, not the stale snapshot.
    assert [el["text"] for el in state.indexed_elements] == ["Address details"]


@pytest.mark.asyncio
async def test_target_text_refused_when_live_screen_cannot_be_observed(monkeypatch):
    executor = _make_executor(width=1000, height=2000)
    state = _indexed_state()
    session_mock = _live_session([], hierarchy_ok=False, ok=False)
    executor._session = session_mock
    monkeypatch.setattr("artemis.mcp.action_executor.asyncio.sleep", AsyncMock())

    res = await executor.execute("click", {"target_text": "Review address"}, "call_blind", state)

    assert res.status == "error"
    assert "could not observe the live screen" in res.text_summary
    session_mock.call.assert_not_called()
