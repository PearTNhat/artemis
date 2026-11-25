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

"""Unit tests for Flash human-in-the-loop choice (P1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from artemis.agents.flash.runner import FlashRunner
from artemis.agents.flash.user_choice import (
    CHOICE_ANSWER_FILENAME,
    PENDING_CHOICE_FILENAME,
    SYSTEM_AUTO_LABEL_PICK,
    SYSTEM_AUTO_LABEL_STUCK,
    build_pending_choice_payload,
    clear_user_choice_files,
    read_pending_choice,
    wait_for_user_choice,
    write_choice_answer,
    write_user_choice_mode,
)
from artemis.agents.validator.tool_declarations import ToolExecutionResult
from artemis.config import AgentGlobalConfig, FlashUserChoiceConfig, load_agent_config
from artemis.context import ArtemisContext
from artemis.data_engine.engine import DataEngine
from artemis.graph.state import State
from artemis.memory.chunking import build_action_ledger
from artemis.runtime import trace_store
from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder


def _make_context(tmp_path: Path | None = None, *, user_choice_enabled: bool = True) -> Mock:
    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = Mock()
    mock_llm_cfg = Mock()
    mock_llm_cfg.model = "gemini-2.5-flash"
    mock_llm_cfg.temperature = 0.1
    ctx.llm_config.get_agent.return_value = mock_llm_cfg
    ctx.device = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    ctx.adb_client = None
    ctx.driver = Mock()
    ctx.actuator = None
    ctx.step_memory = None
    ctx.execution_setup = None
    ctx.user_choice_enabled = user_choice_enabled
    ctx.ask_on_choice = None
    ctx.ask_when_stuck = None
    ctx.agent_config = None
    if tmp_path is not None:
        engine = Mock()
        engine.base_dir = tmp_path
        engine.current_session_id = "sess-test-1"
        engine.current_step_id = 1
        engine.session_start_time = 1000.0
        engine._published = []
        engine._publish = lambda evt, data: engine._published.append((evt, data))
        engine.allocate_step_id = lambda: 1
        engine._recorded_steps = []

        def _record_step(**kwargs):
            step_id = len(engine._recorded_steps) + 1
            engine._recorded_steps.append({"step_number": step_id, "timestamp": 1001.0, **kwargs})
            return step_id

        engine.record_step = _record_step
        engine.record_trace = Mock()
        ctx.data_engine = engine
    else:
        ctx.data_engine = None
    return ctx


# ---------------------------------------------------------------------------
# P1.1 Config tests
# ---------------------------------------------------------------------------


def test_config_backward_compatibility_and_jsonc_files():
    old_cfg = AgentGlobalConfig.model_validate({"flash": {"max_turns": 10}})
    assert old_cfg.flash.user_choice == FlashUserChoiceConfig()
    assert old_cfg.flash.user_choice.enabled is True
    assert old_cfg.flash.user_choice.ask_on_choice is True
    assert old_cfg.flash.user_choice.ask_when_stuck is True
    assert old_cfg.flash.user_choice.stuck_after_no_progress_turns == 2
    assert old_cfg.flash.user_choice.max_options == 8

    repo_root = Path(__file__).resolve().parents[3]
    for name in ("artemis.jsonc", "artemis.qwen.jsonc", "artemis.gemini.jsonc"):
        cfg = load_agent_config(repo_root / "config" / name)
        assert cfg.flash.user_choice.enabled is True
        assert cfg.flash.user_choice.max_options == 8


def test_agent_config_builder_user_choice_overrides():
    builder = AgentConfigBuilder()
    builder.with_flash_user_choice(enabled=False, ask_on_choice=False, ask_when_stuck=True)
    cfg = builder.build(validate_profiles=False)
    assert cfg.flash.user_choice.enabled is False
    assert cfg.flash.user_choice.ask_on_choice is False
    assert cfg.flash.user_choice.ask_when_stuck is True


# ---------------------------------------------------------------------------
# P1.3 & P1.6 user_choice.py & SSE events
# ---------------------------------------------------------------------------


def test_build_pending_choice_payload_system_options():
    pick = build_pending_choice_payload(
        choice_id="c_1",
        session_id="s1",
        kind="pick_item",
        question="Which item?",
        options=[{"id": "1", "label": "Item A"}, {"id": "2", "label": "Item B"}],
    )
    assert [o["id"] for o in pick["options"]] == ["1", "2", "auto", "custom", "manual"]
    assert pick["options"][2]["label"] == SYSTEM_AUTO_LABEL_PICK

    stuck = build_pending_choice_payload(
        choice_id="c_2",
        session_id="s1",
        kind="stuck",
        question="What next?",
        options=[{"id": "1", "label": "Open menu"}],
    )
    assert stuck["options"][1]["label"] == SYSTEM_AUTO_LABEL_STUCK


@pytest.mark.asyncio
async def test_wait_for_user_choice_answers_mismatch_injected_and_events(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    sid = "sess-choice-1"
    trace_dir = tmp_path / sid
    trace_store.init_trace(sid, "goal", "flash")

    published: list[tuple[str, dict]] = []
    engine = SimpleNamespace(_publish=lambda evt, data: published.append((evt, data)))

    # 1. Option answer (preceded by mismatched choice_id that must be ignored)
    async def driver_option():
        while read_pending_choice(trace_dir) is None:
            await asyncio.sleep(0.01)
        pending = read_pending_choice(trace_dir)
        status_during = trace_store.read_status(sid)
        assert status_during["status"] == "running"
        assert status_during["awaiting_user"] is True
        assert status_during["pending_choice"]["choice_id"] == pending["choice_id"]

        # Wrong choice_id ignored
        write_choice_answer(trace_dir, {"choice_id": "wrong_id", "type": "auto"})
        await asyncio.sleep(0.05)
        assert read_pending_choice(trace_dir) is not None

        # Valid option
        write_choice_answer(
            trace_dir, {"choice_id": pending["choice_id"], "type": "option", "option_id": "2"}
        )

    task = asyncio.create_task(driver_option())
    ans, waited_s = await wait_for_user_choice(
        trace_dir=trace_dir,
        session_id=sid,
        kind="pick_item",
        question="Pick one",
        options=[{"id": "1", "label": "A"}, {"id": "2", "label": "B", "detail": "100k"}],
        data_engine=engine,
        poll_interval_s=0.01,
    )
    await task
    assert ans["type"] == "option"
    assert ans["option_id"] == "2"
    assert ans["label"] == "B"
    assert waited_s >= 0.0
    assert not (trace_dir / PENDING_CHOICE_FILENAME).exists()
    assert not (trace_dir / CHOICE_ANSWER_FILENAME).exists()
    status_after = trace_store.read_status(sid)
    assert status_after["status"] == "running"
    assert status_after["awaiting_user"] is False
    assert published[0][0] == "user_choice_requested"
    assert published[0][1]["session_id"] == sid
    assert published[-1][0] == "user_choice_resolved"
    assert published[-1][1]["session_id"] == sid

    # 2. auto, custom, manual, and injected_instruction -> custom
    for payload, expected_type in [
        ({"type": "auto"}, "auto"),
        ({"type": "custom", "text": "cheapest"}, "custom"),
        ({"type": "manual"}, "manual"),
    ]:
        async def _send(p=payload):
            while read_pending_choice(trace_dir) is None:
                await asyncio.sleep(0.01)
            cid = read_pending_choice(trace_dir)["choice_id"]
            write_choice_answer(trace_dir, {"choice_id": cid, **p})

        t = asyncio.create_task(_send())
        res, _ = await wait_for_user_choice(
            trace_dir=trace_dir,
            session_id=sid,
            kind="pick_item",
            question="Pick",
            options=[{"id": "1", "label": "A"}, {"id": "2", "label": "B"}],
            poll_interval_s=0.01,
        )
        await t
        assert res["type"] == expected_type

    async def _send_injected():
        while read_pending_choice(trace_dir) is None:
            await asyncio.sleep(0.01)
        (trace_dir / "injected_instruction.json").write_text(
            json.dumps({"instruction": "pick the blue one", "status": "pending"}), encoding="utf-8"
        )

    t_inj = asyncio.create_task(_send_injected())
    res_inj, _ = await wait_for_user_choice(
        trace_dir=trace_dir,
        session_id=sid,
        kind="pick_item",
        question="Pick",
        options=[{"id": "1", "label": "A"}, {"id": "2", "label": "B"}],
        poll_interval_s=0.01,
    )
    await t_inj
    assert res_inj["type"] == "custom"
    assert res_inj["text"] == "pick the blue one"


@pytest.mark.asyncio
async def test_wait_for_user_choice_long_wait_and_cancellation_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    sid = "sess-cancel"
    trace_dir = tmp_path / sid
    trace_dir.mkdir(parents=True)
    # Deliberately NO status.json: wait_for_user_choice must not create one.

    wait_task = asyncio.create_task(
        wait_for_user_choice(
            trace_dir=trace_dir,
            session_id=sid,
            kind="stuck",
            question="Stuck?",
            options=[{"id": "1", "label": "Retry"}],
            poll_interval_s=0.01,
        )
    )
    # Wait several poll cycles -> must keep waiting, never auto-timeout
    await asyncio.sleep(0.15)
    assert not wait_task.done()
    assert read_pending_choice(trace_dir) is not None
    assert not (trace_dir / "status.json").exists()

    wait_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait_task

    assert not (trace_dir / PENDING_CHOICE_FILENAME).exists()
    assert not (trace_dir / CHOICE_ANSWER_FILENAME).exists()


# ---------------------------------------------------------------------------
# P1.4 & P1.4b FlashRunner behavior
# ---------------------------------------------------------------------------


def test_tool_bound_only_when_enabled():
    with patch("artemis.controllers.unified_controller.get_driver"):
        r_on = FlashRunner(_make_context(user_choice_enabled=True), goal="g")
        r_off = FlashRunner(_make_context(user_choice_enabled=False), goal="g")
    assert "ask_user_choice" in [t.name for t in r_on._get_tools()]
    assert "ask_user_choice" not in [t.name for t in r_off._get_tools()]


@pytest.mark.asyncio
async def test_runner_burst_switches_text_only_validation_antirepeat_and_compression(tmp_path):
    ctx = _make_context(tmp_path=tmp_path, user_choice_enabled=True)
    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(ctx, goal="Buy headphones")
    runner._user_choice_poll_interval_s = 0.01
    runner._reobserve_after_resume = AsyncMock(return_value=(b"POST", '[1] "Tai nghe A" 0,0,10,10'))
    runner.executor.execute = AsyncMock(
        return_value=ToolExecutionResult(
            tool_call_id="tc_click", tool_name="click", status="success", text_summary="Clicked."
        )
    )
    runner._capture_post_screenshot = AsyncMock(return_value=b"AFTER_CLICK")

    state = State.initial("Buy headphones")
    state.latest_screenshot = "shot.jpg"

    # 1. User switch off for pick_item -> error, no wait, burst aborted
    write_user_choice_mode(tmp_path, ask_on_choice=False, ask_when_stuck=True)
    msgs: list = []
    turn = runner._TurnRecord() if hasattr(runner, "_TurnRecord") else None
    from artemis.agents.flash.runner import _TurnRecord

    turn = _TurnRecord()
    res, _, _, _ = await runner._process_tool_calls(
        [
            {"name": "ask_user_choice", "args": {"kind": "pick_item", "question": "Which?", "options": [{"label": "A"}, {"label": "B"}]}, "id": "tc1"},
            {"name": "click", "args": {"target": 1}, "id": "tc2"},
        ],
        state, msgs, "thinking", {"prompt_tokens": 10}, b"PRE", "xml", 0, turn,
    )
    assert res is None
    assert len(msgs) == 2
    assert msgs[0].status == "error" and "Choose yourself" in msgs[0].content
    assert msgs[1].status == "error" and "skipped" in msgs[1].content.lower()

    # 2. User switch off for stuck -> error with report_task_status instruction
    write_user_choice_mode(tmp_path, ask_on_choice=True, ask_when_stuck=False)
    msgs = []
    await runner._process_tool_calls(
        [{"name": "ask_user_choice", "args": {"kind": "stuck", "question": "Stuck?", "options": [{"label": "Open menu"}]}, "id": "tc1"}],
        state, msgs, "thinking", {"prompt_tokens": 10}, b"PRE", "xml", 0, _TurnRecord(),
    )
    assert msgs[0].status == "error" and "Do not tap at random" in msgs[0].content

    # 3. Text-only turn under image_on_demand -> error + _force_image=True
    write_user_choice_mode(tmp_path, ask_on_choice=True, ask_when_stuck=True)
    runner.image_on_demand = True
    runner._text_only_turn = True
    runner._force_image = False
    msgs = []
    await runner._process_tool_calls(
        [
            {"name": "ask_user_choice", "args": {"kind": "stuck", "question": "Stuck?", "options": [{"label": "Open menu"}]}, "id": "tc1"},
            {"name": "click", "args": {"target": 1}, "id": "tc2"},
        ],
        state, msgs, "thinking", {"prompt_tokens": 10}, b"PRE", "xml", 0, _TurnRecord(),
    )
    assert runner._force_image is True
    assert msgs[0].status == "error" and "not seen the screenshot" in msgs[0].content
    assert msgs[1].status == "error" and "skipped" in msgs[1].content.lower()
    runner.image_on_demand = False
    runner._text_only_turn = False

    # 4. Validation: pick_item with 1 option -> error; stuck with 1 option -> valid
    msgs = []
    await runner._process_tool_calls(
        [{"name": "ask_user_choice", "args": {"kind": "pick_item", "question": "Which?", "options": [{"label": "Only one"}]}, "id": "tc1"}],
        state, msgs, "thinking", {"prompt_tokens": 10}, b"PRE", "xml", 0, _TurnRecord(),
    )
    assert msgs[0].status == "error" and "at least 2" in msgs[0].content

    # 5. Valid pick_item wait + burst skip + _force_image + durable history + anti-repeat
    async def answer_first():
        while read_pending_choice(tmp_path) is None:
            await asyncio.sleep(0.01)
        cid = read_pending_choice(tmp_path)["choice_id"]
        write_choice_answer(tmp_path, {"choice_id": cid, "type": "option", "option_id": "1"})

    runner._turn_timing = {"prep_s": 0.1, "llm_s": 0.2}
    runner._turn_timing_pending = True
    runner._force_image = False
    t_ans = asyncio.create_task(answer_first())
    msgs = []
    turn_ok = _TurnRecord()
    await runner._process_tool_calls(
        [
            {
                "name": "ask_user_choice",
                "args": {
                    "kind": "pick_item",
                    "question": "Pick headphone?",
                    "options": [
                        {"label": "Tai nghe A", "detail": "199k"},
                        {"label": "Tai nghe B", "detail": "349k"},
                    ],
                },
                "id": "tc_ask",
            },
            {"name": "click", "args": {"target": 1}, "id": "tc_tap"},
        ],
        state, msgs, "asking user", {"prompt_tokens": 10}, b"PRE", "xml", 0, turn_ok,
    )
    await t_ans
    assert len(msgs) == 2
    assert msgs[0].status == "success"
    assert "Tai nghe A" in msgs[0].content and "199k" in msgs[0].content
    assert "target_text=" not in msgs[0].content
    assert msgs[1].status == "error" and "ended to ask the user" in msgs[1].content
    assert runner._force_image is True
    assert runner._turn_ended_by_user_choice is True
    assert "user_wait_s" in runner._turn_timing
    assert len(turn_ok.step_keys) == 1

    # Verify chunking keeps Asked user (...) verbatim
    ledger_text = build_action_ledger(ctx.data_engine._recorded_steps, session_start=1000.0)
    assert 'Asked user (pick_item): "Pick headphone?" -> User chose option 1: "Tai nghe A" (199k)' in ledger_text

    # Anti-repeat: re-asking exact same labels before any action is blocked
    msgs = []
    await runner._process_tool_calls(
        [
            {
                "name": "ask_user_choice",
                "args": {
                    "kind": "pick_item",
                    "question": "Pick headphone?",
                    "options": [{"label": "tai nghe a..."}, {"label": "TAI NGHE B"}],
                },
                "id": "tc_repeat",
            }
        ],
        state, msgs, "asking again", {"prompt_tokens": 10}, b"PRE", "xml", 0, _TurnRecord(),
    )
    assert msgs[0].status == "error" and "You just asked this" in msgs[0].content

    # After a successful device action, asking again is allowed
    msgs = []
    await runner._process_tool_calls(
        [{"name": "click", "args": {"target": 1}, "id": "tc_act"}],
        state, msgs, "clicking", {"prompt_tokens": 10}, b"PRE", "xml", 0, _TurnRecord(),
    )
    assert runner._acted_since_last_choice is True

    async def answer_stuck_single():
        while read_pending_choice(tmp_path) is None:
            await asyncio.sleep(0.01)
        cid = read_pending_choice(tmp_path)["choice_id"]
        write_choice_answer(tmp_path, {"choice_id": cid, "type": "auto"})

    t_stuck = asyncio.create_task(answer_stuck_single())
    msgs = []
    await runner._process_tool_calls(
        [{"name": "ask_user_choice", "args": {"kind": "stuck", "question": "Try?", "options": [{"label": "Open menu"}]}, "id": "tc_s1"}],
        state, msgs, "stuck", {"prompt_tokens": 10}, b"PRE", "xml", 0, _TurnRecord(),
    )
    await t_stuck
    assert msgs[0].status == "success"
    assert "ONLY those: 'Open menu'" in msgs[0].content
    assert runner._controlled_try is True
    assert runner._try_budget == 2


@pytest.mark.asyncio
async def test_max_turns_does_not_count_answered_choice_and_turns_jsonl_unique(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_DEBUG_AI_IO", "true")
    ctx = _make_context(tmp_path=tmp_path, user_choice_enabled=True)
    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(ctx, goal="g", max_turns=2)
    runner._user_choice_poll_interval_s = 0.01
    runner._reobserve_after_resume = AsyncMock(return_value=(b"POST", '[1] "Item" 0,0,10,10'))
    runner.executor.execute = AsyncMock(
        return_value=ToolExecutionResult(
            tool_call_id="tc", tool_name="click", status="success", text_summary="Clicked."
        )
    )
    runner._capture_post_screenshot = AsyncMock(return_value=b"AFTER")

    bound_tools_seen: list[list[str]] = []
    responses = [
        AIMessage(
            content="Asking user.",
            tool_calls=[
                {
                    "name": "ask_user_choice",
                    "args": {"kind": "stuck", "question": "How?", "options": [{"label": "Step A"}]},
                    "id": "tc_ask",
                }
            ],
        ),
        AIMessage(
            content="Acting on answer.",
            tool_calls=[{"name": "click", "args": {"target": 1}, "id": "tc_click"}],
        ),
        AIMessage(
            content="Reporting.",
            tool_calls=[{"name": "report_task_status", "args": {"status": "completed", "explanation": "done"}, "id": "tc_done"}],
        ),
    ]

    async def fake_invoke(llm, current_tools, messages):
        bound_tools_seen.append([t.name for t in current_tools])
        return responses.pop(0)

    runner._invoke_model = fake_invoke

    async def answer_task():
        while read_pending_choice(tmp_path) is None:
            await asyncio.sleep(0.01)
        cid = read_pending_choice(tmp_path)["choice_id"]
        write_choice_answer(tmp_path, {"choice_id": cid, "type": "option", "option_id": "1"})

    ans_fut = asyncio.create_task(answer_task())
    with patch("artemis.agents.flash.runner.capture_screenshot_and_parse_ui", AsyncMock(return_value=("s.jpg", b"INIT", "xml0"))):
        result = await runner.run(State.initial("g"))
    await ans_fut

    assert result["status"] == "completed"
    # Turn 1 (ask) did NOT consume counted_turns -> Turn 2 still had click (not report_only), Turn 3 was final report_only
    assert "click" in bound_tools_seen[0]
    assert "click" in bound_tools_seen[1]
    assert bound_tools_seen[2] == ["report_task_status"]

    turn_lines = [
        json.loads(line)
        for line in (tmp_path / "turns.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [rec["turn"] for rec in turn_lines] == [1, 2, 3]
    assert "user_wait_s" in turn_lines[0]


def test_controlled_try_budget_and_no_progress_detector(tmp_path):
    from artemis.agents.flash.runner import _TurnRecord

    ctx = _make_context(tmp_path=tmp_path, user_choice_enabled=True)
    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(ctx, goal="g")

    # 1. Helper / wait turns must NOT trigger no_progress
    notices: list[str] = []
    for _ in range(3):
        t = _TurnRecord(interaction_tools=[], device_actions_called=False)
        runner._update_turn_choice_progress(
            turn=t, before_bytes=b"A", before_xml="x", after_bytes=b"A", after_xml="x", pending_notices=notices
        )
    assert notices == []

    # 2. Controlled try with 3 steps -> budget 6; helper/wait doesn't consume; 6 device actions exhausts
    runner._controlled_try = True
    runner._allowed_stuck_steps = ["S1", "S2", "S3"]
    runner._try_budget = 6
    runner._update_turn_choice_progress(
        turn=_TurnRecord(device_actions_called=False),
        before_bytes=b"A", before_xml="x", after_bytes=b"A", after_xml="x", pending_notices=notices,
    )
    assert runner._try_budget == 6
    for i in range(6):
        runner._update_turn_choice_progress(
            turn=_TurnRecord(interaction_tools=["click"], device_actions_called=True),
            before_bytes=b"A", before_xml=f"x{i}", after_bytes=b"B", after_xml=f"x{i+1}", pending_notices=notices,
        )
    assert runner._controlled_try is False
    assert any("6 turns the user allowed" in n for n in notices)

    # 3. Backtrack (click -> press_key BACK -> click -> press_key BACK) triggers notice even if screen changed
    notices.clear()
    runner._no_progress_turns = 0
    runner._update_turn_choice_progress(
        turn=_TurnRecord(interaction_tools=["click"], device_actions_called=True),
        before_bytes=b"A", before_xml="x1", after_bytes=b"B", after_xml="x2", pending_notices=notices,
    )
    assert runner._no_progress_turns == 0
    runner._update_turn_choice_progress(
        turn=_TurnRecord(interaction_tools=["press_key"], device_actions_called=True, backtrack_called=True),
        before_bytes=b"B", before_xml="x2", after_bytes=b"A", after_xml="x1", pending_notices=notices,
    )
    assert runner._no_progress_turns == 1
    runner._update_turn_choice_progress(
        turn=_TurnRecord(interaction_tools=["press_key"], device_actions_called=True, backtrack_called=True),
        before_bytes=b"B", before_xml="x2", after_bytes=b"A", after_xml="x1", pending_notices=notices,
    )
    assert runner._no_progress_turns == 2
    assert any("No progress for 2 turns" in n and "kind='stuck'" in n for n in notices)

    # 4. Controlled try active + 2 no-progress turns -> stronger notice
    notices.clear()
    runner._no_progress_turns = 0
    runner._controlled_try = True
    runner._allowed_stuck_steps = ["S1", "S2", "S3"]
    runner._try_budget = 6
    for _ in range(2):
        runner._update_turn_choice_progress(
            turn=_TurnRecord(interaction_tools=["click"], device_actions_called=True, had_no_effect=True),
            before_bytes=b"A", before_xml="x", after_bytes=b"A", after_xml="x", pending_notices=notices,
        )
    assert any("The steps the user allowed did not help" in n for n in notices)


# ---------------------------------------------------------------------------
# P1.7 DataEngine orphan cleanup
# ---------------------------------------------------------------------------


def test_data_engine_start_session_cleans_orphan_choice_files(tmp_path):
    from artemis.context import DeviceContext, ExecutionSetup

    sid = "11111111-1111-1111-1111-111111111111"
    session_dir = tmp_path / sid
    session_dir.mkdir(parents=True)
    (session_dir / PENDING_CHOICE_FILENAME).write_text("{}", encoding="utf-8")
    (session_dir / CHOICE_ANSWER_FILENAME).write_text("{}", encoding="utf-8")

    ctx = ArtemisContext(
        device=DeviceContext(device_id="dev-1"),
        execution_setup=ExecutionSetup(traces_path=tmp_path),
    )
    engine = DataEngine(ctx)
    engine.start_session("test goal", session_id=sid)

    assert not (session_dir / PENDING_CHOICE_FILENAME).exists()
    assert not (session_dir / CHOICE_ANSWER_FILENAME).exists()


def test_device_lock_does_not_expire_while_pid_alive_and_llm_timeout_only_wraps_llm(tmp_path, monkeypatch):
    """P1.6: DeviceExecutionLock keeps live PID locks indefinitely; invoke_llm_with_timeout_message only wraps LLM calls."""
    import artemis.agents.flash.runner as runner_mod
    from artemis.runtime.device_lock import DeviceExecutionLock

    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda name="": tmp_path / name)
    lock = DeviceExecutionLock(device_id="dev-long-wait", session_id="sess-wait", ingress="mcp")
    lock.acquire(timeout=2)
    assert lock.path.exists()
    try:
        payload = json.loads(lock.path.read_text(encoding="utf-8"))
        # Simulate an old acquired_at (> 1 hour ago) with the current live process PID
        payload["acquired_at"] = "2020-01-01T00:00:00+00:00"
        lock.path.write_text(json.dumps(payload), encoding="utf-8")
        # Stale check must NOT evict the lock while owner PID is still alive
        assert lock._remove_stale_lock() is False
        assert lock.path.exists()
    finally:
        lock.release()

    # Wait for user choice calls FlashRunner._handle_ask_user_choice without going through
    # invoke_llm_with_timeout_message (the LLM wrapper is only used inside _invoke_model).
    import inspect

    handle_src = inspect.getsource(runner_mod.FlashRunner._handle_ask_user_choice)
    invoke_src = inspect.getsource(runner_mod.FlashRunner._invoke_model)
    assert "invoke_llm_with_timeout_message" not in handle_src
    assert "invoke_llm_with_timeout_message" in invoke_src


@pytest.mark.asyncio
async def test_wait_tools_do_not_unlock_repeat_choice_and_release_loop_preserved(tmp_path):
    """Review fixes: wait_* does not set _acted_since_last_choice, and injected release_loop is preserved."""
    from artemis.agents.flash.runner import _TurnRecord
    from artemis.agents.flash.user_choice import set_cli_choice_prompt

    ctx = _make_context(tmp_path=tmp_path, user_choice_enabled=True)
    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(ctx, goal="Monitor updates")
    runner._user_choice_poll_interval_s = 0.01
    runner._reobserve_after_resume = AsyncMock(return_value=(b"POST", '[1] "OK" 0,0,10,10'))

    async def fake_wait_execute(**kwargs):
        return ToolExecutionResult(
            tool_call_id=kwargs["tool_call_id"],
            tool_name=kwargs["name"],
            status="success",
            text_summary="No screen change within timeout.",
        )

    runner.executor.execute = AsyncMock(side_effect=fake_wait_execute)
    runner._capture_post_screenshot = AsyncMock(return_value=b"SAME")

    # Test injected_instruction with release_loop=True while waiting for user choice
    async def inject_release():
        while read_pending_choice(tmp_path) is None:
            await asyncio.sleep(0.01)
        (tmp_path / "injected_instruction.json").write_text(
            json.dumps({"instruction": "Finish now", "release_loop": True, "status": "pending"}),
            encoding="utf-8",
        )

    inj_task = asyncio.create_task(inject_release())
    msgs: list[ToolMessage] = []
    set_cli_choice_prompt(None)
    _, img, xml, seq = await runner._process_tool_calls(
        [
            {
                "name": "ask_user_choice",
                "args": {"kind": "pick_item", "question": "Which?", "options": [{"label": "A"}, {"label": "B"}]},
                "id": "tc_ask_1",
            }
        ],
        State.initial("Monitor updates"),
        msgs,
        "thinking",
        {},
        b"PRE",
        '[1] "A" 0,0,10,10',
        0,
        _TurnRecord(),
    )
    await inj_task
    assert runner._acted_since_last_choice is False
    assert "stop any continuous/monitoring loop" in msgs[0].content
    assert "[release_loop]" in ctx.data_engine._recorded_steps[-1]["extra_metadata"]["injected_instruction"]

    # Execute wait_for_screen_change (succeeds) -> must NOT unlock repeating identical ask_user_choice
    msgs_wait: list[ToolMessage] = []
    _, img, xml, seq = await runner._process_tool_calls(
        [{"name": "wait_for_screen_change", "args": {"timeout_s": 5}, "id": "tc_wait"}],
        State.initial("Monitor updates"),
        msgs_wait,
        "waiting",
        {},
        img,
        xml,
        seq,
        _TurnRecord(),
    )
    assert runner._acted_since_last_choice is False

    msgs_repeat: list[ToolMessage] = []
    await runner._process_tool_calls(
        [
            {
                "name": "ask_user_choice",
                "args": {"kind": "pick_item", "question": "Which?", "options": [{"label": "A"}, {"label": "B"}]},
                "id": "tc_ask_2",
            }
        ],
        State.initial("Monitor updates"),
        msgs_repeat,
        "retry",
        {},
        img,
        xml,
        seq,
        _TurnRecord(),
    )
    assert msgs_repeat[0].status == "error"
    assert "Act on it." in msgs_repeat[0].content


@pytest.mark.asyncio
async def test_executor_repeated_tap_sets_structured_ineffective_tap_flag(tmp_path):
    """Review fix #2: McpActionExecutor returns metadata['ineffective_tap']=True without fragile text matching."""
    from unittest.mock import MagicMock

    from artemis.agents.flash.runner import _TurnRecord
    from artemis.mcp.action_executor import McpActionExecutor

    ctx = _make_context(tmp_path=tmp_path, user_choice_enabled=True)
    executor = McpActionExecutor(ctx)
    executor._session_or_start = AsyncMock(return_value=MagicMock())
    executor._last_point_action = {
        "raw_name": "click",
        "target_coordinates": [500, 500],
        "screen_unchanged": True,
    }
    res = await executor.execute(
        name="click",
        args={"target": [505, 505], "target_description": "Same icon"},
        tool_call_id="tc_rep",
        state=State.initial("g"),
    )
    assert res.status == "error"
    assert res.metadata["ineffective_tap"] is True

    # FlashRunner uses metadata['ineffective_tap'] even with arbitrary wording
    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(ctx, goal="g")
    runner.executor.execute = AsyncMock(
        return_value=ToolExecutionResult(
            tool_call_id="tc_rep",
            tool_name="click",
            status="error",
            text_summary="Custom localized rejection text without keywords.",
            metadata={"ineffective_tap": True},
        )
    )
    turn = _TurnRecord()
    msgs: list[ToolMessage] = []
    await runner._process_tool_calls(
        [{"name": "click", "args": {"target": [505, 505]}, "id": "tc_rep"}],
        State.initial("g"),
        msgs,
        "",
        {},
        b"PRE",
        '[1] "A" 0,0,10,10',
        0,
        turn,
    )
    assert turn.had_ineffective_tap is True


@pytest.mark.asyncio
async def test_cli_standalone_and_daemon_interactive_choice_handlers(tmp_path):
    """P4 CLI fix: standalone TTY prompt handler and daemon interactive waiter resolve choices cleanly."""
    from rich.console import Console

    from artemis.agents.flash.user_choice import (
        format_pending_choice_prompt,
        parse_choice_input,
        set_cli_choice_prompt,
    )
    from artemis.interfaces.cli.commands.run import _wait_daemon_with_interactive_choices

    # 1. Standalone CLI prompt hook unblocks wait_for_user_choice via choice_answer.json
    captured_prompts: list[str] = []

    def fake_prompt(pending: dict) -> str:
        captured_prompts.append(format_pending_choice_prompt(pending))
        return "2"

    set_cli_choice_prompt(fake_prompt)
    try:
        answer, _ = await wait_for_user_choice(
            trace_dir=tmp_path,
            session_id="cli-sess-1",
            kind="pick_item",
            question="Chon mon nao?",
            options=[{"id": "1", "label": "Com tam"}, {"id": "2", "label": "Pho bo"}],
            poll_interval_s=0.01,
        )
    finally:
        set_cli_choice_prompt(None)

    assert answer["type"] == "option"
    assert answer["option_id"] == "2"
    assert answer["label"] == "Pho bo"
    assert captured_prompts and "2. Pho bo" in captured_prompts[0]

    # 2. Daemon mode detects awaiting choice, submits via CLI input, then completes
    submitted: list[tuple[str, dict]] = []
    pending_obj = build_pending_choice_payload(
        choice_id="c-cli-daemon",
        session_id="sess-d",
        kind="pick_item",
        question="Pick size",
        options=[{"id": "1", "label": "M"}, {"id": "2", "label": "L"}],
    )
    sessions_seq = [
        {"session_id": "sess-d", "status": "running"},
        {"session_id": "sess-d", "status": "running"},
        {"session_id": "sess-d", "status": "completed"},
    ]
    pending_state = {"val": pending_obj}

    def fake_submit(sid: str, payload: dict, base_url: str | None = None) -> bool:
        submitted.append((sid, payload))
        pending_state["val"] = None
        return True

    res = _wait_daemon_with_interactive_choices(
        "sess-d",
        base_url="http://127.0.0.1:7860",
        console=Console(quiet=True),
        interactive_tty=True,
        get_session_fn=lambda sid, base_url=None: sessions_seq.pop(0) if len(sessions_seq) > 1 else sessions_seq[0],
        get_status_fn=lambda base_url=None: {},
        get_pending_fn=lambda sid, base_url=None: pending_state["val"],
        submit_choice_fn=fake_submit,
        format_prompt_fn=format_pending_choice_prompt,
        parse_input_fn=parse_choice_input,
        poll_interval=0.01,
        stdin_readline_fn=lambda _prompt: "auto",
    )
    assert res["status"] == "completed"
    assert len(submitted) == 1
    assert submitted[0][1]["type"] == "auto"

    # 3. CLI prompt text polish checks (proper accents + manual instructions)
    formatted = format_pending_choice_prompt(pending_obj)
    assert "[làm xong trên điện thoại rồi gõ m/manual]" in formatted
    assert "[nhập chữ bất kỳ]" in formatted


@pytest.mark.asyncio
async def test_cli_no_ask_user_daemon_flags_unbounded_wait_and_stop_event_cleanup(tmp_path):
    """CLI fixes 1-3: --no-ask-user/non-TTY daemon flags, timeout paused while awaiting user, and stop_event stops stdin thread."""
    import threading
    import time
    from rich.console import Console

    from artemis.agents.flash.user_choice import (
        format_pending_choice_prompt,
        parse_choice_input,
        set_cli_choice_prompt,
    )
    from artemis.interfaces.cli.commands.run import (
        _resolve_daemon_user_choice_flags,
        _wait_daemon_with_interactive_choices,
    )

    # Fix 1: --no-ask-user or non-TTY forces ask_on_choice=False, ask_when_stuck=False
    assert _resolve_daemon_user_choice_flags(
        no_ask_user=True, ask_on_choice=True, ask_when_stuck=True, stdin_isatty=True
    ) == (False, False)
    assert _resolve_daemon_user_choice_flags(
        no_ask_user=False, ask_on_choice=True, ask_when_stuck=True, stdin_isatty=False
    ) == (False, False)
    assert _resolve_daemon_user_choice_flags(
        no_ask_user=False, ask_on_choice=True, ask_when_stuck=False, stdin_isatty=True
    ) == (True, False)

    # Fix 2: Time spent awaiting user choice does NOT count toward daemon timeout
    fake_now = {"t": 0.0}
    pending_box = {
        "val": build_pending_choice_payload(
            choice_id="c-long-cli",
            session_id="sess-long",
            kind="pick_item",
            question="Wait indefinitely?",
            options=[{"id": "1", "label": "Yes"}, {"id": "2", "label": "No"}],
        )
    }
    ticks = {"n": 0}

    def fake_session(sid: str, base_url: str | None = None):
        return {"session_id": sid, "status": "completed" if ticks["n"] >= 4 else "running"}

    def fake_sleep(dt: float) -> None:
        ticks["n"] += 1
        # Simulate 1000s passing while awaiting user input vs 1s during active running
        fake_now["t"] += 1000.0 if pending_box["val"] is not None else 1.0
        if ticks["n"] == 3:
            pending_box["val"] = None

    res_long = _wait_daemon_with_interactive_choices(
        "sess-long",
        base_url="http://127.0.0.1:7860",
        console=Console(quiet=True),
        interactive_tty=False,
        get_session_fn=fake_session,
        get_status_fn=lambda base_url=None: {},
        get_pending_fn=lambda sid, base_url=None: pending_box["val"],
        submit_choice_fn=lambda *_a, **_kw: True,
        format_prompt_fn=format_pending_choice_prompt,
        parse_input_fn=parse_choice_input,
        timeout=10.0,
        poll_interval=1.0,
        clock_fn=lambda: fake_now["t"],
        sleep_fn=fake_sleep,
    )
    assert res_long["status"] == "completed"

    # Active running time without pending choice still respects timeout
    fake_now["t"] = 0.0
    res_to = _wait_daemon_with_interactive_choices(
        "sess-timeout",
        base_url="http://127.0.0.1:7860",
        console=Console(quiet=True),
        interactive_tty=False,
        get_session_fn=lambda sid, base_url=None: {"session_id": sid, "status": "running"},
        get_status_fn=lambda base_url=None: {},
        get_pending_fn=lambda sid, base_url=None: None,
        submit_choice_fn=lambda *_a, **_kw: True,
        format_prompt_fn=format_pending_choice_prompt,
        parse_input_fn=parse_choice_input,
        timeout=10.0,
        poll_interval=6.0,
        clock_fn=lambda: fake_now["t"],
        sleep_fn=lambda dt: fake_now.__setitem__("t", fake_now["t"] + dt),
    )
    assert res_to["status"] == "timeout"

    # Fix 3: 2-arg standalone CLI prompt exits promptly via stop_event when resolved externally
    exited = threading.Event()

    def slow_stdin_prompt(pending: dict, stop_event: threading.Event) -> str | None:
        while not stop_event.is_set():
            time.sleep(0.02)
        exited.set()
        return None

    async def inject_external():
        while read_pending_choice(tmp_path) is None:
            await asyncio.sleep(0.01)
        (tmp_path / "injected_instruction.json").write_text(
            json.dumps({"instruction": "Resolved from Web UI", "status": "pending"}),
            encoding="utf-8",
        )

    set_cli_choice_prompt(slow_stdin_prompt)
    try:
        inj = asyncio.create_task(inject_external())
        ans, _ = await wait_for_user_choice(
            trace_dir=tmp_path,
            session_id="cli-stop-event",
            kind="pick_item",
            question="Pick",
            options=[{"id": "1", "label": "A"}, {"id": "2", "label": "B"}],
            poll_interval_s=0.01,
        )
        await inj
    finally:
        set_cli_choice_prompt(None)

    assert ans["text"] == "Resolved from Web UI"
    assert exited.is_set()


# --- Structural "comparable items" hint -------------------------------------

def _el(text, bounds, cls="android.widget.TextView", **extra):
    return {"text": text, "bounds": list(bounds), "class": cls, **extra}


def test_find_comparable_items_detects_two_column_product_grid():
    from artemis.agents.flash.user_choice import find_comparable_items

    els = [
        _el("Search results", (0, 100, 1080, 180)),
        _el("Wireless earbuds model alpha 5.3", (20, 600, 530, 680)),
        _el("Wireless earbuds model beta pro", (550, 600, 1060, 680)),
        _el("Over-ear headphones gamma noise", (20, 1300, 530, 1380)),
        _el("Sport neckband delta waterproof", (550, 1300, 1060, 1380)),
        _el("125.000", (20, 700, 200, 740)),  # short price label: ignored
    ]
    texts = find_comparable_items(els, 1080, 2400)
    assert len(texts) == 4
    assert "Wireless earbuds model alpha 5.3" in texts


def test_find_comparable_items_ignores_single_row_tabs_and_short_labels():
    from artemis.agents.flash.user_choice import find_comparable_items

    tabs = [
        _el("Recommended for you today", (0, 300, 360, 360)),
        _el("Best sellers of this week", (360, 300, 720, 360)),
        _el("New arrivals for this season", (720, 300, 1080, 360)),
    ]
    assert find_comparable_items(tabs, 1080, 2400) == []  # one row only

    short = [_el(t, (0, y, 1080, y + 80)) for t, y in (("Wi-Fi", 200), ("Bluetooth", 300), ("Display", 400))]
    assert find_comparable_items(short, 1080, 2400) == []

    ocr = [
        _el("Wireless earbuds model alpha 5.3", (20, y, 530, y + 80), is_ocr=True)
        for y in (600, 1300, 2000)
    ]
    assert find_comparable_items(ocr, 1080, 2400) == []


def test_comparable_items_notice_mentions_tool_and_examples():
    from artemis.agents.flash.user_choice import comparable_items_notice

    text = comparable_items_notice(["Item one long name", "Item two long name", "Item three long name", "x" * 80])
    assert "ask_user_choice" in text and "pick_item" in text
    assert "(4," in text and "'Item one long name'" in text


def test_comparable_items_notice_never_quotes_developer_ids():
    from artemis.agents.flash.user_choice import comparable_items_notice, looks_like_identifier

    ids = ["dd_module_product_card_0", "dd_module_product_card_1", "com.app:id/item_title"]
    assert all(looks_like_identifier(t) for t in ids)
    assert not looks_like_identifier("Wireless earbuds model alpha")
    text = comparable_items_notice(ids)
    assert "dd_module" not in text and "item_title" not in text
    assert "screenshot" in text and "(3)" in text


def test_id_only_cards_take_their_inner_title_and_skeletons_are_skipped():
    from artemis.agents.flash.user_choice import find_comparable_items

    cards = [
        _el("dd_module_product_card_0", (20, 533, 489, 873), cls="android.view.ViewGroup"),
        _el("dd_module_product_card_1", (510, 533, 979, 880), cls="android.view.ViewGroup"),
        _el("dd_module_product_card_2", (20, 900, 489, 1240), cls="android.view.ViewGroup"),
        _el("dd_module_product_card_3", (510, 900, 979, 1240), cls="android.view.ViewGroup"),
    ]
    titles = [
        _el("Wireless earbuds model alpha 5.3", (30, 760, 480, 800)),
        _el("Wireless earbuds model beta pro", (520, 760, 970, 800)),
        _el("Over-ear headphones gamma noise", (30, 1130, 480, 1170)),
        _el("-50%", (900, 540, 970, 560)),
    ]
    texts = find_comparable_items(cards + titles, 1080, 2400)
    assert texts == [
        "Wireless earbuds model alpha 5.3",
        "Wireless earbuds model beta pro",
        "Over-ear headphones gamma noise",
    ]
    # Still loading: cards without readable titles never trigger the hint.
    assert find_comparable_items(cards, 1080, 2400) == []


def test_card_title_prefers_letters_over_rating_and_strips_separators():
    from artemis.agents.flash.user_choice import find_comparable_items

    cards = [
        _el(f"dd_module_product_card_{i}", (20 + 490 * (i % 2), 533 + 400 * (i // 2), 489 + 490 * (i % 2), 900 + 400 * (i // 2)), cls="android.view.ViewGroup")
        for i in range(4)
    ]
    inner = []
    for i, title in enumerate(["Cable alpha fast charge", "Cable beta braided long", "Cable gamma short spare"]):
        x = 30 + 490 * (i % 2)
        y = 760 + 400 * (i // 2)
        inner.append(_el(f"!!{title}!!", (x, y, x + 440, y + 40)))
        inner.append(_el("!!!!4.9!780,2k ratings!", (x, y + 50, x + 440, y + 80)))
    texts = find_comparable_items(cards + inner, 1080, 2400)
    assert texts == ["Cable alpha fast charge", "Cable beta braided long", "Cable gamma short spare"]


# --- form_options: radio / checkbox groups ----------------------------------

def test_form_options_validation_allows_full_group_and_multi_select():
    from artemis.agents.flash.user_choice import validate_ask_user_choice_args

    opts = [{"label": f"Topping {i}", "detail": f"+{i}.000"} for i in range(1, 15)]
    ok, err, norm = validate_ask_user_choice_args(
        {"kind": "form_options", "question": "Toppings?", "options": opts, "multi_select": True, "max_select": 3},
        max_options=8,
    )
    assert ok, err
    assert len(norm["options"]) == 14  # whole group, not capped at 8
    assert norm["multi_select"] is True and norm["max_select"] == 3
    ok, _, norm = validate_ask_user_choice_args(
        {"kind": "form_options", "question": "Size?", "options": opts[:3]}, max_options=8
    )
    assert ok and norm["multi_select"] is False


def test_multi_answer_validation_and_cli_parse():
    from artemis.agents.flash.user_choice import (
        _validate_answer_for_pending,
        build_pending_choice_payload,
        parse_choice_input,
    )

    pending = build_pending_choice_payload(
        choice_id="c1", session_id="s", kind="form_options", question="Toppings?",
        options=[{"id": "1", "label": "Cheese"}, {"id": "2", "label": "Egg"}, {"id": "3", "label": "Bacon"}],
        multi_select=True, max_select=2,
    )
    ok = _validate_answer_for_pending({"choice_id": "c1", "type": "options", "option_ids": ["1", "3"]}, pending)
    assert ok["labels"] == ["Cheese", "Bacon"]
    assert _validate_answer_for_pending({"choice_id": "c1", "type": "options", "option_ids": ["1", "2", "3"]}, pending) is None
    assert _validate_answer_for_pending({"choice_id": "c1", "type": "options", "option_ids": ["9"]}, pending) is None
    assert parse_choice_input("1, 3", pending) == {"choice_id": "c1", "type": "options", "option_ids": ["1", "3"]}
    assert parse_choice_input("2", pending)["type"] == "option"

    single = dict(pending, multi_select=False)
    assert _validate_answer_for_pending({"choice_id": "c1", "type": "options", "option_ids": ["1"]}, single) is None
    assert parse_choice_input("1,3", single)["type"] == "custom"


def test_find_selectable_group_radio_checkbox_and_row_labels():
    from artemis.agents.flash.user_choice import find_selectable_group

    radios = [
        _el(t, (40, y, 1000, y + 80), cls="android.widget.RadioButton", checkable="true",
            checked="true" if i == 0 else "false")
        for i, (t, y) in enumerate((("Size M", 600), ("Size L", 700), ("Size XL", 800)))
    ]
    multi, labels = find_selectable_group(radios)
    assert multi is False and labels == ["Size M (selected)", "Size L", "Size XL"]

    boxes = [_el("", (900, y, 980, y + 60), cls="android.widget.CheckBox", checkable="true") for y in (1000, 1100)]
    names = [_el("Extra cheese", (40, 1005, 600, 1055)), _el("Fried egg", (40, 1105, 600, 1155))]
    multi, labels = find_selectable_group(boxes + names)
    assert multi is True and labels == ["Extra cheese", "Fried egg"]

    toggles = [_el(t, (40, y, 1000, y + 80), cls="android.widget.Switch", checkable="true") for t, y in (("Wi-Fi", 300), ("Bluetooth", 400))]
    assert find_selectable_group(toggles) == (None, [])


def test_selectable_group_notice_teaches_form_options():
    from artemis.agents.flash.user_choice import selectable_group_notice

    text = selectable_group_notice(True, ["Extra cheese", "Fried egg"])
    assert "form_options" in text and "multi_select=true" in text and "EVERY option" in text


def test_completion_refused_while_users_pick_is_open():
    from artemis.agents.flash.runner import FlashRunner

    r = FlashRunner.__new__(FlashRunner)
    r._open_choice_kind = None
    r._open_choice_rejections = 0
    msgs: list = []
    calls = [{"id": "a", "name": "report_task_status"}, {"id": "b", "name": "click"}]
    assert r._reject_completion_with_open_choice({"status": "completed"}, "a", calls, 0, msgs) is False

    r._open_choice_kind = "pick_item"
    assert r._reject_completion_with_open_choice({"status": "failed"}, "a", calls, 0, msgs) is False
    assert r._reject_completion_with_open_choice({"status": "completed"}, "a", calls, 0, msgs) is True
    assert len(msgs) == 2 and "not picked yet" in msgs[0].content and msgs[1].tool_call_id == "b"
    assert r._reject_completion_with_open_choice({"status": "completed"}, "a", calls, 0, []) is True
    # Never trap the model: the third attempt goes through.
    assert r._reject_completion_with_open_choice({"status": "completed"}, "a", calls, 0, []) is False


def test_user_answer_summary_wording():
    from artemis.agents.flash.user_choice import user_answer_summary

    assert user_answer_summary({"type": "option", "option_id": "2", "label": "Cable B"}) == "2. Cable B"
    assert user_answer_summary({"type": "options", "labels": ["Cheese", "Egg"]}) == "Cheese, Egg"
    assert "xem thêm" in user_answer_summary({"type": "custom", "text": "xem thêm"})


def test_custom_drawn_tick_boxes_form_a_group():
    """Pick-N lists whose tick boxes are plain unlabeled buttons (no checkable flag)."""
    from artemis.agents.flash.user_choice import find_selectable_group

    W, H = 1280, 2772
    def px(l, t, r, b):  # 0-1000 grid -> pixels
        return (l * W // 1000, t * H // 1000, r * W // 1000, b * H // 1000)
    els = [
        _el("Item list", px(40, 389, 251, 432)),
        _el("Pick 3", px(849, 398, 939, 419)),
        _el("Milk tea pearl size M", px(172, 446, 718, 475)),
        _el("Kumquat tea size L", px(172, 498, 644, 526)),
        _el("checkbox_tick", px(71, 554, 111, 573), cls="android.widget.ImageView"),
        _el("Oolong jasmine milk tea M", px(172, 550, 839, 578)),
        _el("", px(30, 432, 152, 484), cls="android.view.ViewGroup", is_hint=True),
        _el("", px(30, 484, 152, 536), cls="android.view.ViewGroup", is_hint=True),
        _el("", px(40, 68, 142, 115), cls="android.view.ViewGroup", is_hint=True),  # back button, alone
    ]
    multi, labels = find_selectable_group(els, width=W, height=H)
    assert multi is None
    assert labels == ["Milk tea pearl size M", "Kumquat tea size L", "Oolong jasmine milk tea M"]
