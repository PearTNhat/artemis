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

"""Tests for Admin Console user-choice API endpoints, multi-device status, and stop cleanup."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from apps.admin_console.core.state import state
from apps.admin_console.server import app
from apps.admin_console.services.task_queue_service import AdbEndpoint, AdbTarget, TaskQueueService
from artemis.agents.flash.user_choice import (
    CHOICE_ANSWER_FILENAME,
    PENDING_CHOICE_FILENAME,
    USER_CHOICE_MODE_FILENAME,
    _atomic_write_json,
    build_pending_choice_payload,
    read_user_choice_mode,
)
from artemis.runtime import DeviceExecutionLock, trace_store


@pytest.mark.asyncio
async def test_choice_endpoints_validation_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    sid = "sess-api-1"
    trace_dir = tmp_path / sid
    trace_dir.mkdir(parents=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        # 1. No pending choice -> GET false, POST 409
        res_get = await ac.get(f"/api/tasks/{sid}/choice")
        assert res_get.status_code == 200
        assert res_get.json() == {"session_id": sid, "awaiting_user": False, "pending_choice": None}

        res_409 = await ac.post(
            "/api/tasks/choice",
            json={"session_id": sid, "choice_id": "c_1", "type": "auto"},
        )
        assert res_409.status_code == 409

        # Write pending_choice.json
        pending = build_pending_choice_payload(
            choice_id="c_abc",
            session_id=sid,
            kind="pick_item",
            question="Which one?",
            options=[{"id": "1", "label": "Item 1"}, {"id": "2", "label": "Item 2"}],
        )
        _atomic_write_json(trace_dir / PENDING_CHOICE_FILENAME, pending)

        res_get2 = await ac.get(f"/api/tasks/{sid}/choice")
        assert res_get2.status_code == 200
        assert res_get2.json()["awaiting_user"] is True
        assert res_get2.json()["pending_choice"]["choice_id"] == "c_abc"

        # 2. Mismatched choice_id -> 409
        res_mismatch = await ac.post(
            "/api/tasks/choice",
            json={"session_id": sid, "choice_id": "c_wrong", "type": "auto"},
        )
        assert res_mismatch.status_code == 409

        # 3. Empty custom text -> 400
        res_empty_custom = await ac.post(
            "/api/tasks/choice",
            json={"session_id": sid, "choice_id": "c_abc", "type": "custom", "text": "   "},
        )
        assert res_empty_custom.status_code == 400

        # 4. Invalid option_id -> 400
        res_bad_opt = await ac.post(
            "/api/tasks/choice",
            json={"session_id": sid, "choice_id": "c_abc", "type": "option", "option_id": "99"},
        )
        assert res_bad_opt.status_code == 400

        # 5. Valid option -> 200 + atomic file written
        res_ok = await ac.post(
            "/api/tasks/choice",
            json={"session_id": sid, "choice_id": "c_abc", "type": "option", "option_id": "2"},
        )
        assert res_ok.status_code == 200
        saved = json.loads((trace_dir / CHOICE_ANSWER_FILENAME).read_text(encoding="utf-8"))
        assert saved == {"choice_id": "c_abc", "type": "option", "option_id": "2"}

        # 6. Choice mode switch -> writes user_choice_mode.json
        res_mode = await ac.post(
            "/api/tasks/choice-mode",
            json={"session_id": sid, "ask_on_choice": False, "ask_when_stuck": True},
        )
        assert res_mode.status_code == 200
        assert (trace_dir / USER_CHOICE_MODE_FILENAME).exists()
        assert read_user_choice_mode(trace_dir) == (False, True)


@pytest.mark.asyncio
async def test_status_reports_two_parallel_awaiting_sessions_in_active_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", classmethod(lambda cls: None))

    sid1, sid2 = "sess-dev-1", "sess-dev-2"
    for sid, q in [(sid1, "Pick A or B?"), (sid2, "Stuck on screen?")]:
        d = tmp_path / sid
        d.mkdir(parents=True)
        _atomic_write_json(
            d / PENDING_CHOICE_FILENAME,
            build_pending_choice_payload(
                choice_id=f"c_{sid}",
                session_id=sid,
                kind="pick_item",
                question=q,
                options=[{"id": "1", "label": "X"}, {"id": "2", "label": "Y"}],
            ),
        )

    owners = {
        "dev1": SimpleNamespace(
            device_id="dev1", session_id=sid1, description="g1", pid=111, ingress="frontend", acquired_at=1.0
        ),
        "dev2": SimpleNamespace(
            device_id="dev2", session_id=sid2, description="g2", pid=222, ingress="frontend", acquired_at=2.0
        ),
    }
    monkeypatch.setattr(DeviceExecutionLock, "get_active_owners", classmethod(lambda cls: owners))
    monkeypatch.setattr(DeviceExecutionLock, "get_active_owner", classmethod(lambda cls: owners["dev1"]))
    monkeypatch.setattr(DeviceExecutionLock, "get_queued_tasks", classmethod(lambda cls: []))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        res = await ac.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    by_sid = {t["session_id"]: t for t in data["active_tasks"]}
    assert by_sid[sid1]["awaiting_user"] is True
    assert by_sid[sid1]["pending_choice"]["question"] == "Pick A or B?"
    assert by_sid[sid2]["awaiting_user"] is True
    assert by_sid[sid2]["pending_choice"]["question"] == "Stuck on screen?"


@pytest.mark.asyncio
async def test_run_request_and_worker_flags_for_batch_goals(monkeypatch):
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", classmethod(lambda cls: None))
    monkeypatch.setattr(DeviceExecutionLock, "reserve", classmethod(lambda cls, **kw: "ticket-1"))
    monkeypatch.setattr(TaskQueueService, "_reject_unavailable_device", classmethod(lambda cls, s: _async_none()))
    monkeypatch.setattr(
        "apps.admin_console.routers.tasks.readiness_engine.run_device_submission_probe",
        lambda target_serial=None: _async_none(),
    )
    monkeypatch.setattr(
        "artemis.runtime.device_pool.select_device_async",
        lambda: _async_val("dev-1"),
    )

    old_queue = list(state.queue_items)
    try:
        state.queue_items = []
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
            res = await ac.post(
                "/api/run",
                json={
                    "goals": ["goal 1", "goal 2"],
                    "profile": "flash",
                    "ask_on_choice": False,
                    "ask_when_stuck": True,
                },
            )
        assert res.status_code == 200
        tasks = res.json()["tasks"]
        assert len(tasks) == 2
        for item in tasks:
            assert item["ask_on_choice"] is False
            assert item["ask_when_stuck"] is True
            cmd, _ = TaskQueueService._build_worker_invocation(
                item,
                "run-1",
                item["session_id"],
                item["goal"],
                "flash",
                AdbTarget(endpoint=AdbEndpoint(host="127.0.0.1", port=5037), serial="dev-1"),
            )
            assert "--no-ask-on-choice" in cmd
            assert "--ask-when-stuck" in cmd
    finally:
        state.queue_items = old_queue


def test_stop_cleans_up_user_choice_files(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    sid = "sess-stop-clean"
    trace_dir = tmp_path / sid
    trace_dir.mkdir(parents=True)
    trace_store.init_trace(sid, "goal", "flash")
    _atomic_write_json(trace_dir / PENDING_CHOICE_FILENAME, {"choice_id": "c1"})
    _atomic_write_json(trace_dir / CHOICE_ANSWER_FILENAME, {"choice_id": "c1", "type": "auto"})

    TaskQueueService._clear_session_user_choice(sid)
    assert not (trace_dir / PENDING_CHOICE_FILENAME).exists()
    assert not (trace_dir / CHOICE_ANSWER_FILENAME).exists()


async def _async_none():
    return None


async def _async_val(val):
    return val
