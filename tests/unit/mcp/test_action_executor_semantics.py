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

"""Targets are either observed (an element index) or described (coordinates).

Flash binds the Pro Operator's agent dialect: a click, long press or focused
input names an element index from the indexed UI list -- resolved here to the
element's center with its observed text/bounds/id recorded -- or a coordinate
pair, which the model must describe itself. A coordinate swipe or click
sequence must be described too. Nothing is inferred, the description never
reaches the wire, and the wire only ever sees coordinates.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from artemis.mcp.action_executor import McpActionExecutor, _ArgError
from artemis.mcp.action_types import ActionCode


def _make_executor(width=1080, height=2400):
    ctx = Mock()
    ctx.device.device_width = width
    ctx.device.device_height = height
    actuator = Mock()
    actuator.controller = Mock()
    return McpActionExecutor(ctx, actuator=actuator)


def _state():
    state = Mock()
    state.indexed_elements = []
    state.indexed_points = [[540, 1440], [745, 1440]]
    state.latest_ui_hierarchy = None
    return state


def _indexed_state():
    """Two observed elements (pixel centers, the shape ``observe()`` writes)."""
    state = _state()
    state.indexed_elements = [
        {
            "index": 1,
            "center": [540, 1248],
            "text": "Wi-Fi",
            "bounds": [40, 1152, 1040, 1344],
            "class": "android.widget.TextView",
            "resource_id": "android:id/title",
            "is_ocr": False,
        },
        {
            "index": 2,
            "center": [540, 720],
            "text": "Search settings",
            "bounds": [80, 660, 1000, 780],
            "class": "android.widget.EditText",
            "resource_id": "com.android.settings:id/search",
            "is_ocr": False,
        },
    ]
    state.indexed_points = [el["center"] for el in state.indexed_elements]
    return state


# --- Index targets: resolved against the element list, observed semantics recorded ---


@pytest.mark.parametrize("index", [1, "1", 1.0, [1]])
def test_click_index_resolves_to_the_elements_center_and_records_observed_fields(index):
    executor = _make_executor()
    wire_name, wire_args, _, recorded = executor._translate(
        "click", {"target": index}, _indexed_state()
    )
    assert wire_name == "click"
    # 540/1080 -> 500, 1248/2400 -> 520 (normalized 0-1000)
    assert wire_args == {"target": [500, 520], "times": 1, "delay_ms": 100}
    assert recorded == {
        "target_text": "Wi-Fi",
        "target_bounds": [37, 480, 963, 560],
        "target_resource_id": "android:id/title",
        "target_class": "android.widget.TextView",
    }
    assert "target_description" not in recorded


def test_index_target_needs_no_description_and_ignores_a_stray_one():
    """An index already names its element; the model's belief never mixes with
    the observation on the record."""
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "click", {"target": 2, "target_description": "the search box"}, _indexed_state()
    )
    assert wire_args["target"] == [500, 300]
    assert recorded["target_text"] == "Search settings"
    assert "target_description" not in recorded


def test_long_press_and_input_text_resolve_indices_too():
    executor = _make_executor()
    _, press_args, _, recorded = executor._translate(
        "long_press", {"target": 1, "duration": 1500}, _indexed_state()
    )
    assert press_args == {"target": [500, 520], "duration_ms": 1500}
    assert recorded["target_text"] == "Wi-Fi"

    _, type_args, _, recorded = executor._translate(
        "input_text", {"text": "wifi", "target": 2}, _indexed_state()
    )
    assert type_args == {"text": "wifi", "target": [500, 300], "clear_exist": True}
    assert recorded["target_resource_id"] == "com.android.settings:id/search"


@pytest.mark.parametrize("name, label", [("click", "click"), ("long_press", "long press")])
def test_out_of_range_index_is_refused_with_the_active_range(name, label):
    executor = _make_executor()
    with pytest.raises(_ArgError) as excinfo:
        executor._translate(name, {"target": 7}, _indexed_state())
    message = str(excinfo.value)
    assert message.startswith(f"Error during {label}: Invalid target index 7.")
    assert "1 to 2" in message
    assert "ask_explorer" in message


def test_index_against_an_empty_list_is_refused():
    executor = _make_executor()
    with pytest.raises(_ArgError) as excinfo:
        executor._translate("click", {"target": 1}, _state())
    message = str(excinfo.value)
    assert "Invalid target index 1" in message
    assert "empty" in message


def test_unusable_target_is_refused_with_both_syntaxes_named():
    executor = _make_executor()
    with pytest.raises(_ArgError) as excinfo:
        executor._translate("click", {"target": "the button"}, _indexed_state())
    message = str(excinfo.value)
    assert message.startswith("Error during click: Invalid target format")
    assert "element index" in message and "[x, y]" in message


def test_index_resolution_uses_the_live_device_resolution():
    """The element list is built against the device size ``observe()`` wrote
    on the context; a different resolution maps the same pixels differently."""
    executor = _make_executor(width=720, height=1600)
    state = _indexed_state()
    state.indexed_elements[0]["center"] = [360, 800]
    _, wire_args, _, _ = executor._translate("click", {"target": 1}, state)
    assert wire_args["target"] == [500, 500]


# --- Coordinate targets: the model describes what it aims at -------------------------


@pytest.mark.parametrize(
    "name, args, label",
    [
        ("click", {"target": [500, 600]}, "click"),
        ("long_press", {"target": [500, 600]}, "long press"),
        ("input_text", {"text": "hi", "target": [500, 600]}, "input text"),
        ("swipe", {"start": [200, 600], "end": [800, 600]}, "swipe"),
    ],
)
def test_coordinate_targets_without_description_are_refused(name, args, label):
    executor = _make_executor()
    with pytest.raises(_ArgError) as excinfo:
        executor._translate(name, args, _state())
    message = str(excinfo.value)
    assert message.startswith(f"Error during {label}")
    assert "target_description" in message


@pytest.mark.parametrize("blank", ["", "   ", None, 7])
def test_blank_or_non_string_description_is_refused(blank):
    executor = _make_executor()
    with pytest.raises(_ArgError):
        executor._translate("click", {"target": [500, 600], "target_description": blank}, _state())


def test_description_is_accepted_and_kept_off_the_wire():
    executor = _make_executor()
    wire_name, wire_args, _, _ = executor._translate(
        "click", {"target": [500, 600], "target_description": "play button"}, _state()
    )
    assert wire_name == "click"
    assert wire_args == {"target": [500, 600], "times": 1, "delay_ms": 100}
    assert "target_description" not in wire_args

    _, swipe_args, _, _ = executor._translate(
        "swipe",
        {"start": [200, 600], "end": [800, 600], "target_description": "brightness knob"},
        _state(),
    )
    assert swipe_args == {"start": [200, 600], "end": [800, 600], "duration_ms": 400}


def test_recorded_target_is_the_cleaned_description_in_pro_shape():
    """The recorded semantics come from the same check that requires them."""
    executor = _make_executor()
    _, _, _, recorded = executor._translate(
        "click", {"target": [1, 2], "target_description": " ok "}, _state()
    )
    assert recorded == {"target_description": "ok"}

    _, _, _, recorded = executor._translate(
        "long_press", {"target": [1, 2], "target_description": "thumbnail"}, _state()
    )
    assert recorded == {"target_description": "thumbnail"}

    _, _, _, recorded = executor._translate(
        "click_sequence", {"sequence": [[1, 2]], "target_descriptions": ["a "]}, _state()
    )
    assert recorded == {"target_descriptions": ["a"]}

    # No coordinate target: nothing is recorded, nothing inferred.
    _, _, _, recorded = executor._translate("press_key", {"key": "BACK"}, _state())
    assert recorded == {}


def test_coordinate_swipe_records_its_description():
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "swipe",
        {"start": [200, 600], "end": [800, 600], "target_description": " brightness knob "},
        _state(),
    )
    assert recorded == {"target_description": "brightness knob"}
    assert "target_description" not in wire_args


def test_targeted_input_records_its_description():
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "input_text",
        {"text": "hi", "target": [500, 600], "target_description": "search input"},
        _state(),
    )
    assert wire_args["target"] == [500, 600]
    assert recorded == {"target_description": "search input"}
    assert "target_description" not in wire_args


def test_focused_typing_without_a_target_needs_no_description():
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "input_text", {"text": "hi", "target": None}, _state()
    )
    assert wire_args["target"] is None
    assert recorded == {}


def test_focused_typing_ignores_a_stray_description():
    """Typing into the focused field has no coordinate target: a description the
    model tacked on anyway is not recorded, so no self-described target enters
    the history for an action that aimed at nothing."""
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "input_text",
        {"text": "hi", "target": None, "target_description": "search input"},
        _state(),
    )
    assert wire_args["target"] is None
    assert recorded == {}


def test_directional_swipe_needs_no_description():
    executor = _make_executor()
    wire_name, wire_args, _, recorded = executor._translate("swipe", {"direction": "up"}, _state())
    assert wire_name == "swipe"
    assert "start" in wire_args and "end" in wire_args
    assert "target_description" not in recorded
    # L-030: the step records the swipe actually sent, not the direction default.
    assert recorded["normalized_start_coordinates"] == wire_args["start"]
    assert recorded["normalized_end_coordinates"] == wire_args["end"]
    assert recorded["normalized_coordinates"] == wire_args["start"] + wire_args["end"]


def test_directional_swipe_ignores_a_stray_description():
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "swipe", {"direction": "up", "target_description": "the feed"}, _state()
    )
    assert "target_description" not in wire_args
    assert "target_description" not in recorded


def test_click_sequence_requires_one_description_per_entry():
    executor = _make_executor()
    sequence = [[500, 600], [690, 600], [10, 10]]

    with pytest.raises(_ArgError) as excinfo:
        executor._translate("click_sequence", {"sequence": sequence}, _state())
    assert "target_descriptions" in str(excinfo.value)
    assert "3 expected" in str(excinfo.value)

    # Wrong length is refused too.
    with pytest.raises(_ArgError):
        executor._translate(
            "click_sequence",
            {"sequence": sequence, "target_descriptions": ["video body", "skip"]},
            _state(),
        )

    _, wire_args, _, recorded = executor._translate(
        "click_sequence",
        {"sequence": sequence, "target_descriptions": ["video body", "digit 2", "corner"]},
        _state(),
    )
    assert wire_args["sequence"] == [[500, 600], [690, 600], [10, 10]]
    assert "target_descriptions" not in wire_args
    assert recorded == {"target_descriptions": ["video body", "digit 2", "corner"]}


@pytest.mark.parametrize("index_entry", [2, "2", 2.0, [2]])
def test_click_sequence_refuses_element_indices(index_entry):
    """click_sequence takes coordinate pairs only. An index would be resolved
    against the element list and then recorded under the model's own
    description, reading back as a self-described coordinate target."""
    executor = _make_executor()
    sequence = [[500, 600], index_entry, [10, 10]]

    with pytest.raises(_ArgError) as excinfo:
        executor._translate(
            "click_sequence",
            {"sequence": sequence, "target_descriptions": ["video body", "digit 2", "corner"]},
            _state(),
        )
    message = str(excinfo.value)
    assert message.startswith("Error during click sequence")
    assert "entry 2" in message
    assert "coordinate" in message
    assert "ask_explorer" in message


def test_click_sequence_accepts_a_serialized_pair_list():
    executor = _make_executor()
    _, wire_args, _, _ = executor._translate(
        "click_sequence",
        {"sequence": "[[500, 600], [10, 10]]", "target_descriptions": ["video body", "corner"]},
        _state(),
    )
    assert wire_args["sequence"] == [[500, 600], [10, 10]]


def test_determine_settle_ms_app_agnostic():
    executor = _make_executor()
    # App launch gets 1500ms
    assert executor._determine_settle_ms("manage_app", {}, {}) == 1500
    # Enter / Search key gets 1000ms
    assert executor._determine_settle_ms("press_key", {"key": "ENTER"}, {}) == 1000
    assert executor._determine_settle_ms("press_key", {"keycode": "66"}, {}) == 1000
    # Regular clicks (even with previously Grab-specific keywords like basket, cart, search) get 400ms
    assert executor._determine_settle_ms("click", {"target_description": "basket button"}, {}) == 400
    assert executor._determine_settle_ms("click", {"target_description": "search box"}, {}) == 400
    assert executor._determine_settle_ms("click", {"target_description": "phở tái bò viên"}, {}) == 400


@pytest.mark.asyncio
async def test_repeated_tap_allowed_when_screen_changed_or_after_intervening_action():
    executor = _make_executor()
    executor.actuator.extensions.return_value = []
    state = _state()
    state.indexed_elements = [
        {"index": 1, "text": "Item 1", "center": [100, 100]},
        {"index": 2, "text": "Confirm", "center": [500, 500]},
    ]

    session_mock = Mock()
    session_mock.started = True
    session_mock.call = AsyncMock()
    session_mock.observe = AsyncMock()
    obs_mock = Mock()
    obs_mock.ok = True
    obs_mock.hierarchy_ok = True
    obs_mock.elements = state.indexed_elements
    obs_mock.screenshot_path = None
    obs_mock.elements_text = ""
    obs_mock.scrollable = None
    obs_mock.height = 2400
    session_mock.observe.return_value = obs_mock
    res_mock = Mock()
    res_mock.ok = True
    res_mock.message = "Tapped"
    res_mock.code = ActionCode.OK
    session_mock.call.return_value = res_mock
    executor._session = session_mock

    # Case 1: Prior tap had screen_unchanged = False (e.g. chessboard visual highlight / move changed image)
    executor._last_point_action = None
    res1 = await executor.execute(
        "click",
        {"target": [500, 500], "target_description": "chess tile or button"},
        "call_1",
        state,
    )
    assert res1.status == "success"

    # Case 2: Consecutive duplicate tap with unchanged screen is rejected
    executor._last_point_action = {
        "raw_name": "click",
        "target_coordinates": [500, 500],
        "screen_unchanged": True,
    }
    res_reject = await executor.execute(
        "click",
        {"target": [500, 500], "target_description": "chess tile or button"},
        "call_2",
        state,
    )
    assert res_reject.status == "error"
    assert "Repeated tap rejected" in res_reject.text_summary

    # Case 3: An intervening action (swipe or wait) resets _last_point_action
    await executor.execute(
        "swipe",
        {"direction": "up"},
        "call_swipe",
        state,
    )
    assert executor._last_point_action is None

    # After intervening action, tapping the same point again is now allowed!
    res3 = await executor.execute(
        "click",
        {"target": [500, 500], "target_description": "retry button"},
        "call_3",
        state,
    )
    assert res3.status == "success"


@pytest.mark.asyncio
async def test_screen_transition_retry_when_unchanged_after_tap():
    executor = _make_executor()
    executor.actuator.extensions.return_value = []
    state = _state()
    state.indexed_elements = [{"index": 1, "text": "Basket", "center": [500, 900]}]

    session_mock = Mock()
    session_mock.started = True
    session_mock.call = AsyncMock()
    session_mock.observe = AsyncMock()

    # Initial observation: same screen (Basket button)
    obs1 = Mock(
        ok=True,
        hierarchy_ok=True,
        elements=state.indexed_elements,
        screenshot_path=None,
        elements_text="[1] Basket",
        scrollable=None,
        height=2400,
    )
    # Retry observation after 800ms: cart page opened!
    new_elements = [{"index": 1, "text": "Cart Items", "center": [500, 300]}]
    obs2 = Mock(
        ok=True,
        hierarchy_ok=True,
        elements=new_elements,
        screenshot_path=None,
        elements_text="[1] Cart Items",
        scrollable=None,
        height=2400,
    )
    session_mock.observe.side_effect = [obs1, obs2]
    res_mock = Mock(ok=True, message="Tapped", code=ActionCode.OK)
    session_mock.call.return_value = res_mock
    executor._session = session_mock

    # Set prior signature to match obs1 so first observe looks unchanged
    import hashlib
    parts = [f"{el.get('text')}:{el.get('center')}:{el.get('checked')}:{el.get('selected')}:{el.get('error')}" for el in obs1.elements]
    executor._last_elements_signature = hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()

    res = await executor.execute(
        "click",
        {"target": [500, 900], "target_description": "basket button"},
        "call_tap",
        state,
    )
    assert res.status == "success"
    # observe should have been called twice (initial + 800ms retry)
    assert session_mock.observe.call_count == 2
    # And state was updated with new_elements from obs2
    assert state.indexed_elements == new_elements

