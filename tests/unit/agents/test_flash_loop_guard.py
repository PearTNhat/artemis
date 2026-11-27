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

"""The Flash loop guard stops a model that keeps redoing the same thing."""

from artemis.agents.flash.loop_guard import LoopGuard

PKG = "com.example.app"


def _screen(*labels):
    nodes = [
        {"package": PKG, "class": "android.widget.FrameLayout", "bounds": "[0,0][1000,1000]", "visible-to-user": "true"}
    ] + [
        {
            "package": PKG,
            "class": "android.widget.TextView",
            "bounds": f"[0,{100 * i}][1000,{100 * i + 80}]",
            "visible-to-user": "true",
            "text": t,
        }
        for i, t in enumerate(labels, 1)
    ]
    return {"elements": nodes, "width": 1000, "height": 1000}


def test_same_action_on_same_screen_warns_then_stops():
    g = LoopGuard()
    scr = _screen("Search for a city", "Clear")
    g.note("click", {"target": 4}, scr, [500, 378])
    assert g.pop_notice() is None and g.stop_reason is None
    g.note("click", {"target": 4}, scr, [502, 380])  # same control, a few units off
    g.note("click", {"target": 4}, scr, [500, 378])
    assert "Loop warning" in g.pop_notice()
    assert g.stop_reason is None  # the first tap had a different step before it
    g.note("click", {"target": 4}, scr, [500, 378])
    assert g.stop_reason and "3 times" in g.stop_reason


def test_a_redone_sequence_is_caught_on_its_repeated_steps():
    g = LoopGuard()
    home, search = _screen("Clock", "Tokyo"), _screen("Search for a city")
    for _ in range(3):
        g.note("click", {"target": 11}, home, [500, 738])
        g.note("input_text", {"text": "Helsinki", "target": 2}, search, [582, 88])
    assert g.stop_reason


def test_repetition_that_changes_the_screen_is_not_a_loop():
    g = LoopGuard()
    for qty in range(1, 8):  # tap "+" while the quantity label grows
        g.note("click", {"target": 3}, _screen("Pho", f"Quantity {qty}"), [900, 500])
    assert g.stop_reason is None and g.pop_notice() is None


def test_different_targets_and_waits_do_not_count():
    g = LoopGuard()
    scr = _screen("Settings", "Wi-Fi", "Bluetooth")
    for y in (200, 300, 400):
        g.note("click", {}, scr, [500, y])
    for _ in range(5):
        g.note("wait_for_screen_change", {}, scr)
    assert g.stop_reason is None


def test_no_tree_never_counts():
    g = LoopGuard()
    for _ in range(5):
        g.note("click", {}, None, [500, 500])
    assert g.stop_reason is None


def test_fixing_a_form_then_confirming_again_is_not_a_loop():
    """Tap confirm -> pick an option -> confirm again: the step before differs."""
    g = LoopGuard()
    scr = _screen("Delivery instructions", "Confirm address")
    for opt_y in (300, 400, 500, 600):
        g.note("click", {}, scr, [500, 936])
        g.note("click", {}, scr, [500, opt_y])
    assert g.stop_reason is None


def test_click_sequences_with_different_points_differ():
    g = LoopGuard()
    scr = _screen("Board")
    for i in range(6):
        g.note("click_sequence", {"sequence": [[100 * i, 100], [100, 100 * i]]}, scr)
    assert g.stop_reason is None
