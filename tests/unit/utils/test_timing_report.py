"""Tests for the per-step timing report."""

import json
import sqlite3

from artemis.utils.timing_report import build_timing_report, format_timing_report


def _db(tmp_path):
    path = tmp_path / "engine.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (session_id TEXT, initial_goal TEXT, start_time REAL, status TEXT);
        CREATE TABLE steps (session_id TEXT, step_number INT, timestamp REAL, action_taken TEXT, extra_metadata TEXT);
        CREATE TABLE traces (session_id TEXT, type TEXT, name TEXT, duration REAL);
        """
    )
    conn.execute("INSERT INTO sessions VALUES ('abc123', 'chat with Phuc', 100.0, 'completed')")
    steps = [
        (1, 110.0, {"action": "manage_app", "args": {"app_name": "Telegram"}},
         {"llm_s": 6.0, "prep_s": 0.1, "action_s": 3.0, "observe_s": 0.5}),
        (2, 120.0, {"action": "input_text", "args": {"text": "hi"}},
         {"llm_s": 8.0, "prep_s": 0.2, "action_s": 1.0, "observe_s": 0.4,
          "agy": {"model_calls": 1, "opened_screenshot": False}}),
        (3, 121.5, {"action": "click", "target_text": "Send"},
         {"action_s": 1.0, "observe_s": 0.4, "shared_model_call": True}),
    ]
    for n, ts, action, timing in steps:
        meta = {"timing": timing, "token_usage": {"prompt_tokens": 1000, "completion_tokens": 20}}
        conn.execute(
            "INSERT INTO steps VALUES ('abc123', ?, ?, ?, ?)",
            (n, ts, json.dumps(action), json.dumps(meta)),
        )
    conn.execute("INSERT INTO traces VALUES ('abc123', 'agent', 'hopper', 2.5)")
    conn.commit()
    conn.close()
    return path


def test_report_splits_wall_time(tmp_path):
    report = build_timing_report(_db(tmp_path), "abc")
    assert report["session_id"] == "abc123"
    assert [s["wall_s"] for s in report["steps"]] == [10.0, 10.0, 1.5]
    t = report["totals"]
    assert t["wall_s"] == 21.5
    assert t["llm_s"] == 14.0  # the shared-call step adds no model time
    assert t["action_s"] == 5.0
    assert t["observe_s"] == 1.3
    assert t["other_s"] == round(21.5 - 14.0 - 5.0 - 1.3 - 0.3, 2)
    assert report["agents"][0]["name"] == "hopper"


def test_format_mentions_agy_and_shared_calls(tmp_path):
    text = format_timing_report(build_timing_report(_db(tmp_path), "latest"))
    assert "[agy 1 calls, screenshot no]" in text
    assert "(same)" in text
    assert "model wait" in text and "hopper" in text


def test_unknown_session(tmp_path):
    assert build_timing_report(_db(tmp_path), "zzz") is None
