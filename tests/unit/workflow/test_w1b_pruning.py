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

"""Unit tests for W1b detour cycle pruning and W0 offline evaluator."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3

from artemis.workflow.recorder import RawScreen, RecordedStep, record_run
from scripts.workflow_replay_eval import evaluate_db


PKG = "com.example.app"


def _node(
    rid: str,
    l: int,
    t: int,
    r: int,
    b: int,
    *,
    text: str = "",
    cls: str = "android.widget.Button",
    clickable: bool = True,
    editable: bool = False,
    checked: bool = False,
    selected: bool = False,
) -> dict:
    return {
        "resource-id": f"{PKG}:id/{rid}" if rid else "",
        "class": cls,
        "bounds": f"[{l},{t}][{r},{b}]",
        "clickable": str(clickable).lower(),
        "editable": str(editable).lower(),
        "checked": str(checked).lower(),
        "selected": str(selected).lower(),
        "enabled": "true",
        "visible-to-user": "true",
        "package": PKG,
        "text": text,
    }


def _home(*, toggled: bool = False, extra_city: str = "") -> list[dict]:
    nodes = [
        _node("title", 20, 20, 400, 80, text="Home", cls="android.widget.TextView", clickable=False),
        _node("settings", 800, 20, 980, 80, text="Settings"),
        _node("add", 800, 800, 960, 960, text="Add"),
        _node("toggle", 20, 200, 400, 280, text="Dark", checked=toggled, cls="android.widget.Switch"),
        _node("list", 20, 320, 980, 700, text="Item 1", cls="android.widget.TextView", clickable=False),
    ]
    if extra_city:
        nodes.append(_node("city2", 20, 400, 980, 480, text=extra_city, cls="android.widget.TextView", clickable=False))
    return nodes


def _settings_menu() -> list[dict]:
    return [
        _node("st_title", 20, 20, 400, 80, text="Settings", cls="android.widget.TextView", clickable=False),
        _node("about", 20, 120, 980, 200, text="About"),
        _node("help", 20, 220, 980, 300, text="Help"),
        _node("back_btn", 20, 800, 200, 900, text="Back"),
        _node("footer", 20, 920, 980, 980, text="v1", cls="android.widget.TextView", clickable=False),
    ]


def _about_screen() -> list[dict]:
    return [
        _node("ab_title", 20, 20, 400, 80, text="About", cls="android.widget.TextView", clickable=False),
        _node("ver", 20, 120, 980, 200, text="1.0", cls="android.widget.TextView", clickable=False),
        _node("lic", 20, 220, 980, 300, text="Licenses"),
        _node("back_ab", 20, 800, 200, 900, text="Close"),
        _node("ab_foot", 20, 920, 980, 980, text="Info", cls="android.widget.TextView", clickable=False),
    ]


def _search_screen(typed: str = "") -> list[dict]:
    return [
        _node("s_title", 20, 20, 400, 80, text="Search", cls="android.widget.TextView", clickable=False),
        _node("s_input", 20, 120, 980, 200, text=typed, cls="android.widget.EditText", editable=True),
        _node("s_go", 800, 220, 980, 300, text="Go"),
        _node("s_hint", 20, 350, 980, 420, text="Enter name", cls="android.widget.TextView", clickable=False),
        _node("s_cancel", 20, 800, 200, 900, text="Cancel"),
    ]


def _scr(elems: list[dict], seq: int) -> RawScreen:
    return RawScreen(elems, 1000, 1000, seq=seq)


def _tap(rid: str, pre: RawScreen, post: RawScreen, box: tuple[int, int, int, int]) -> RecordedStep:
    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    return RecordedStep(
        tool="click",
        args={"target": 1},
        action={"normalized_coordinates": [cx, cy], "target_bounds": list(box), "target_resource_id": f"{PKG}:id/{rid}"},
        pre=pre,
        post=post,
    )


def test_w1b_prunes_multi_step_detour_without_slot_and_unchanged_state():
    h0 = _scr(_home(), 1)
    s1 = _scr(_settings_menu(), 2)
    a2 = _scr(_about_screen(), 3)
    s3 = _scr(_settings_menu(), 4)
    h4 = _scr(_home(), 5)  # exact same home state after 4-step detour
    sr5 = _scr(_search_screen(), 6)
    sr6 = _scr(_search_screen("Paris"), 7)

    steps = [
        _tap("settings", h0, s1, (800, 20, 980, 80)),
        _tap("about", s1, a2, (20, 120, 980, 200)),
        _tap("back_ab", a2, s3, (20, 800, 200, 900)),
        _tap("back_btn", s3, h4, (20, 800, 200, 900)),
        _tap("add", h4, sr5, (800, 800, 960, 960)),
        RecordedStep(
            tool="input_text",
            args={"text": "Paris"},
            action={"normalized_coordinates": [500, 160], "target_bounds": [20, 120, 980, 200]},
            pre=sr5,
            post=sr6,
        ),
    ]
    wf = record_run(steps, goal="Add Paris")
    assert wf is not None
    rids = [e.target.resource_id.split("/")[-1] for e in wf.edges if e.target]
    assert rids == ["add", "s_input"]


def test_w1b_keeps_cycle_when_element_state_changed():
    h0 = _scr(_home(toggled=False), 1)
    s1 = _scr(_settings_menu(), 2)
    h2 = _scr(_home(toggled=True), 3)  # checked state changed on return!
    sr3 = _scr(_search_screen(), 4)
    sr4 = _scr(_search_screen("Paris"), 5)

    steps = [
        _tap("settings", h0, s1, (800, 20, 980, 80)),
        _tap("back_btn", s1, h2, (20, 800, 200, 900)),
        _tap("add", h2, sr3, (800, 800, 960, 960)),
        RecordedStep(
            tool="input_text",
            args={"text": "Paris"},
            action={"normalized_coordinates": [500, 160], "target_bounds": [20, 120, 980, 200]},
            pre=sr3,
            post=sr4,
        ),
    ]
    wf = record_run(steps, goal="Toggle dark and add Paris")
    assert wf is not None
    rids = [e.target.resource_id.split("/")[-1] for e in wf.edges if e.target]
    assert "settings" in rids and "back_btn" in rids


def test_w1b_keeps_cycle_when_slot_entered_inside():
    h0 = _scr(_home(), 1)
    sr1 = _scr(_search_screen(), 2)
    sr2 = _scr(_search_screen("Paris"), 3)
    h3 = _scr(_home(extra_city="Paris"), 4)

    steps = [
        _tap("add", h0, sr1, (800, 800, 960, 960)),
        RecordedStep(
            tool="input_text",
            args={"text": "Paris"},
            action={"normalized_coordinates": [500, 160], "target_bounds": [20, 120, 980, 200]},
            pre=sr1,
            post=sr2,
        ),
        _tap("s_cancel", sr2, h3, (20, 800, 200, 900)),
    ]
    wf = record_run(steps, goal="Add Paris")
    assert wf is not None
    assert len(wf.edges) == 3


def test_w0_eval_read_only_on_minimal_db(tmp_path: Path):
    import json

    db = tmp_path / "ro.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (session_id TEXT, start_time REAL, initial_goal TEXT, status TEXT, device_info TEXT)")
    conn.execute("CREATE TABLE steps (step_id INTEGER, session_id TEXT, step_number INTEGER, pre_image_name TEXT, post_image_name TEXT, action_taken TEXT, last_execution_result TEXT)")
    conn.execute("CREATE TABLE images (image_name TEXT, ui_tree TEXT)")
    imgs = {"h": _home(), "s": _search_screen(), "s_p": _search_screen("Paris"), "s_r": _search_screen("Rome"), "a": _about_screen()}
    for k, v in imgs.items():
        conn.execute("INSERT INTO images VALUES (?, ?)", (k, json.dumps(v)))
    for sid, city, t in [("s1", "Paris", 1.0), ("s2", "Rome", 2.0)]:
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?, 'completed', ?)", (sid, t, f"Add {city}", json.dumps({"device_width": 1000, "device_height": 1000})))
        conn.execute("INSERT INTO steps VALUES (1, ?, 1, 'h', 's', ?, ?)", (sid, json.dumps({"action": "click", "normalized_coordinates": [880, 880], "target_bounds": [800, 800, 960, 960], "target_resource_id": f"{PKG}:id/add"}), json.dumps({"status": "dispatched", "result": "ok"})))
        s_after = "s_p" if city == "Paris" else "s_r"
        conn.execute("INSERT INTO steps VALUES (2, ?, 2, 's', ?, ?, ?)", (sid, s_after, json.dumps({"action": "input_text", "args": {"text": city}, "normalized_coordinates": [500, 160], "target_bounds": [20, 120, 980, 200]}), json.dumps({"status": "dispatched", "result": "ok"})))
        conn.execute("INSERT INTO steps VALUES (3, ?, 3, ?, 'a', ?, ?)", (sid, s_after, json.dumps({"action": "click", "normalized_coordinates": [890, 260], "target_bounds": [800, 220, 980, 300], "target_resource_id": f"{PKG}:id/s_go"}), json.dumps({"status": "dispatched", "result": "ok"})))
    conn.commit()
    conn.close()
    before_bytes = db.read_bytes()
    stats = asyncio.run(evaluate_db(db))
    assert db.read_bytes() == before_bytes
    assert stats[PKG].autopilot_steps == 3
    assert stats[PKG].undetected_mismatches == 0
