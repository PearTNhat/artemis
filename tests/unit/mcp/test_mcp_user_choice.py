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

"""Unit tests for P3 MCP integration of Flash ask_user_choice."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from artemis.agents.flash.user_choice import (
    CHOICE_ANSWER_FILENAME,
    PENDING_CHOICE_FILENAME,
    USER_CHOICE_MODE_FILENAME,
    _atomic_write_json,
    _dispatch_needs_input_notification,
    build_needs_input_message,
    build_pending_choice_payload,
    read_user_choice_mode,
)
from artemis.runtime import trace_store
from mcp_server.tools import task_manager, task_runner


def _setup_trace_dir(tmp_path: Path, monkeypatch, trace_id: str, *, status_data: dict) -> Path:
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    monkeypatch.setattr(task_manager, "_find_data_engine_db", lambda: None)
    monkeypatch.setattr(task_manager, "_session_tracked_by_lock", lambda _tid: False)
    trace_dir = tmp_path / trace_id
    trace_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(trace_dir / "status.json", status_data)
    return trace_dir


def test_mobile_run_task_forwards_user_choice_flags_daemon_and_standalone(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    monkeypatch.delenv("ARTEMIS_STANDALONE", raising=False)

    def fake_submit(**kwargs):
        return {"tasks": [{"session_id": kwargs["session_id"]}]}

    daemon_submit = MagicMock(side_effect=fake_submit)
    with (
        patch.object(task_runner, "ensure_daemon_running", return_value=(True, "http://127.0.0.1:9999")),
        patch.object(task_runner, "submit_task_to_daemon", daemon_submit),
    ):
        res = task_runner.mobile_run_task(
            task_desc="Do something",
            conversation_id="conv-123",
            model="Flash",
            ask_on_choice=False,
            ask_when_stuck=True,
        )

    assert res["status"] == "running"
    assert daemon_submit.call_args.kwargs["ask_on_choice"] is False
    assert daemon_submit.call_args.kwargs["ask_when_stuck"] is True

    # Standalone subprocess fallback
    popen_mock = MagicMock()
    popen_mock.return_value.pid = os.getpid()
    lock_cls_mock = MagicMock()
    lock_cls_mock.reserve.return_value = "ticket-1"
    lock_cls_mock.QUEUE_TICKET_ENV = "ARTEMIS_QUEUE_TICKET"
    with (
        patch.object(task_runner, "ensure_daemon_running", return_value=(False, None)),
        patch.object(task_runner, "DeviceExecutionLock", lock_cls_mock),
        patch.object(task_runner.subprocess, "Popen", popen_mock),
        patch.object(task_runner.threading, "Thread", return_value=MagicMock()),
        patch.object(task_runner, "read_ipc_port", return_value=0),
    ):
        res2 = task_runner.mobile_run_task(
            task_desc="Standalone test",
            conversation_id="conv-456",
            model="Flash",
            ask_on_choice=True,
            ask_when_stuck=False,
        )

    assert res2["message"] == "Successfully started background task."
    cmd = popen_mock.call_args.args[0]
    env = popen_mock.call_args.kwargs["env"]
    assert "--ask-on-choice" in cmd
    assert "--no-ask-when-stuck" in cmd
    assert env["ARTEMIS_CONVERSATION_ID"] == "conv-456"


def test_mobile_manage_task_status_awaiting_user_and_dead_pid_reconcile(tmp_path, monkeypatch):
    tid_alive = "trace_alive_wait"
    pending = build_pending_choice_payload(
        choice_id="c-1",
        session_id=tid_alive,
        kind="pick_item",
        question="Pick one",
        options=[{"label": "A"}, {"label": "B"}],
    )
    tdir = _setup_trace_dir(
        tmp_path,
        monkeypatch,
        tid_alive,
        status_data={
            "trace_id": tid_alive,
            "status": "running",
            "pid": os.getpid(),
            "start_time": 100.0,
            "awaiting_user": True,
            "pending_choice": pending,
        },
    )
    _atomic_write_json(tdir / PENDING_CHOICE_FILENAME, pending)

    res = task_manager.mobile_manage_task(action="status", trace_id=tid_alive)
    assert res["status"] == "awaiting_user"
    assert res["pending_choice"]["choice_id"] == "c-1"
    # On-disk status.json stays "running"
    assert json.loads((tdir / "status.json").read_text(encoding="utf-8"))["status"] == "running"

    # Dead runner PID while awaiting user must still reconcile to "failed"
    tid_dead = "trace_dead_wait"
    _setup_trace_dir(
        tmp_path,
        monkeypatch,
        tid_dead,
        status_data={
            "trace_id": tid_dead,
            "status": "running",
            "pid": 99999999,
            "start_time": 1.0,
            "awaiting_user": True,
            "pending_choice": pending,
        },
    )
    monkeypatch.setattr(task_manager, "_pid_alive", lambda _pid: False)
    res_dead = task_manager.mobile_manage_task(action="status", trace_id=tid_dead)
    assert res_dead["status"] == "failed"


def test_mobile_manage_task_answer_choice_set_mode_inject_and_stop(tmp_path, monkeypatch):
    tid = "trace_manage_choice"
    pending = build_pending_choice_payload(
        choice_id="c-99",
        session_id=tid,
        kind="stuck",
        question="Cannot find menu",
        options=[{"id": "1", "label": "Open drawer"}],
    )
    tdir = _setup_trace_dir(
        tmp_path,
        monkeypatch,
        tid,
        status_data={
            "trace_id": tid,
            "status": "running",
            "pid": os.getpid(),
            "start_time": 100.0,
            "awaiting_user": True,
            "pending_choice": pending,
        },
    )

    # Graceful message when no pending_choice.json exists yet
    no_pending = task_manager.mobile_manage_task(
        action="answer_choice", trace_id=tid, instruction="1"
    )
    assert "No pending user choice" in no_pending["message"]

    _atomic_write_json(tdir / PENDING_CHOICE_FILENAME, pending)

    # Option index
    r1 = task_manager.mobile_manage_task(action="answer_choice", trace_id=tid, instruction="1")
    assert r1["status"] == "running"
    assert json.loads((tdir / CHOICE_ANSWER_FILENAME).read_text(encoding="utf-8")) == {
        "choice_id": "c-99",
        "type": "option",
        "option_id": "1",
    }

    # Auto
    r2 = task_manager.mobile_manage_task(action="answer_choice", trace_id=tid, instruction="auto")
    assert r2["answer"]["type"] == "auto"

    # Manual
    r3 = task_manager.mobile_manage_task(action="answer_choice", trace_id=tid, instruction="manual")
    assert r3["answer"]["type"] == "manual"

    # Custom text
    r4 = task_manager.mobile_manage_task(
        action="answer_choice", trace_id=tid, instruction="Tap More then Settings"
    )
    assert r4["answer"] == {
        "choice_id": "c-99",
        "type": "custom",
        "text": "Tap More then Settings",
    }

    # set_choice_mode writes user_choice_mode.json
    r_mode = task_manager.mobile_manage_task(
        action="set_choice_mode",
        trace_id=tid,
        ask_on_choice=False,
        ask_when_stuck=True,
    )
    assert r_mode["ask_on_choice"] is False
    assert r_mode["ask_when_stuck"] is True
    assert (tdir / USER_CHOICE_MODE_FILENAME).exists()
    assert read_user_choice_mode(tdir) == (False, True)

    # inject_instruction keeps working while awaiting_user=True because storage status == running
    with patch.object(task_manager, "is_daemon_running", return_value=False):
        r_inj = task_manager.mobile_manage_task(
            action="inject_instruction", trace_id=tid, instruction="Focus on top tab"
        )
    assert "Successfully injected instruction" in r_inj["message"]

    # stop clears pending_choice.json & choice_answer.json
    with (
        patch.object(task_manager, "is_daemon_running", return_value=False),
        patch.object(task_manager.os, "kill", lambda *_a, **_kw: None),
    ):
        r_stop = task_manager.mobile_manage_task(action="stop", trace_id=tid)
    assert r_stop["status"] == "cancelled"
    assert not (tdir / PENDING_CHOICE_FILENAME).exists()
    assert not (tdir / CHOICE_ANSWER_FILENAME).exists()
    final_status = json.loads((tdir / "status.json").read_text(encoding="utf-8"))
    assert final_status["awaiting_user"] is False
    assert final_status["pending_choice"] is None


def test_needs_input_message_and_notifier_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    monkeypatch.setenv("ARTEMIS_CONVERSATION_ID", "conv-notify")

    stuck_pending = build_pending_choice_payload(
        choice_id="c-stuck",
        session_id="trace-notify",
        kind="stuck",
        question="Cannot find profile icon",
        options=[{"label": "Open drawer"}],
    )
    msg_stuck = build_needs_input_message("trace-notify", stuck_pending)
    assert "ONLY the listed steps above within a fixed turn budget" in msg_stuck
    assert 'mobile_manage_task(action="answer_choice", trace_id="trace-notify"' in msg_stuck

    pick_pending = build_pending_choice_payload(
        choice_id="c-pick",
        session_id="trace-notify",
        kind="pick_item",
        question="Which variant?",
        options=[{"label": "Small"}, {"label": "Large"}],
    )
    msg_pick = build_needs_input_message("trace-notify", pick_pending)
    assert "let AI pick the best option for the goal" in msg_pick

    notify_mock = MagicMock()
    with patch("mcp_server.notifiers.notify", notify_mock):
        _dispatch_needs_input_notification("trace-notify", stuck_pending)

    notify_mock.assert_called_once()
    kwargs = notify_mock.call_args.kwargs
    assert kwargs["conversation_id"] == "conv-notify"
    assert kwargs["event_type"] == "needs_input"
    assert kwargs["payload"]["trace_id"] == "trace-notify"
    assert kwargs["payload"]["choice_id"] == "c-stuck"
