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

"""turns.jsonl: one line per Flash turn, controlled by AI I/O debug setting."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from artemis.agents.flash.runner import FlashRunner
from artemis.utils import turn_log


def _runner(tmp_path):
    runner = FlashRunner.__new__(FlashRunner)
    runner._llm = SimpleNamespace(model="gemini-3.5-flash-lite")
    runner.ctx = SimpleNamespace(data_engine=SimpleNamespace(base_dir=tmp_path))
    runner.compact_list = True
    runner.media_resolution = "low"
    runner._turn_timing = {"llm_s": 2.0, "prep_s": 0.1}
    return runner


def _response():
    return SimpleNamespace(usage_metadata={
        "input_tokens": 1000, "output_tokens": 50,
        "input_token_details": {"cache_read": 600}, "output_token_details": {"reasoning": 30},
    })


def _click_result():
    return SimpleNamespace(
        status="success", text_summary="Clicked [18] 'Pho'.",
        metadata={"code": "ok", "target_coordinates": [145, 100], "no_effect": False,
                  "timed_out": False, "action_s": 0.8, "observe_s": 0.5},
    )


def test_disabled_by_default_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_log, "is_ai_io_debug_enabled", lambda: False)
    runner = _runner(tmp_path)
    runner._start_turn_record(1, _response(), "[1] \"x\" 0,0,1,1")
    assert runner._turn_rec is None
    runner._write_turn_record(runner._turn_timing, 3.0, 0.8, 0.5)
    assert not (tmp_path / "turns.jsonl").exists()


def test_enabled_writes_one_line_per_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_log, "is_ai_io_debug_enabled", lambda: True)
    runner = _runner(tmp_path)
    runner._start_turn_record(1, _response(), "[1] \"x\" 0,0,1,1")
    runner._turn_rec["actions"].append(turn_log.action_entry("click", {"target": 18}, _click_result()))
    runner._write_turn_record(runner._turn_timing, 3.6, 0.8, 0.5)

    lines = (tmp_path / "turns.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["turn"] == 1 and rec["model"] == "gemini-3.5-flash-lite"
    assert rec["llm_s"] == 2.0 and rec["in_tok"] == 1000 and rec["cached_tok"] == 600
    assert rec["out_tok"] == 50 and rec["reasoning_tok"] == 30
    assert rec["list_format"] == "compact" and rec["media_resolution"] == "low"
    assert rec["total_s"] == 3.6 and rec["other_s"] == 0.2
    action = rec["actions"][0]
    assert action == {"tool": "click", "args": {"target": 18}, "status": "success", "code": "ok",
                      "tap_0_1000": [145, 100], "action_s": 0.8, "observe_s": 0.5,
                      "msg": "Clicked [18] 'Pho'."}
    assert runner._turn_rec is None


def test_ai_io_debug_controls_turn_log(monkeypatch):
    monkeypatch.setattr(turn_log, "is_ai_io_debug_enabled", lambda: False)
    assert not turn_log.turn_log_enabled()
    monkeypatch.setattr(turn_log, "is_ai_io_debug_enabled", lambda: True)
    assert turn_log.turn_log_enabled()


def test_long_text_is_cut_and_no_effect_is_kept():
    result = SimpleNamespace(status="success", text_summary="x" * 500,
                             metadata={"no_effect": True})
    entry = turn_log.action_entry("input_text", {"text": "y" * 400}, result)
    assert len(entry["args"]["text"]) == 161 and len(entry["msg"]) == 161
    assert entry["no_effect"] is True


def test_append_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert turn_log.append_turn(blocker / "sub", {"turn": 1}) is None
    assert turn_log.append_turn(None, {"turn": 1}) is None
