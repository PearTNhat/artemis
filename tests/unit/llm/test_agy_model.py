"""Tests for the agy CLI chat model (no real agy process)."""

import base64
import json
import os
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from artemis.core.tool_declaration import ToolDeclaration
from artemis.llm import agy_model
from artemis.llm.agy_model import PROTOCOL_HEADER, AgyChatModel, parse_reply
from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider

CLICK = ToolDeclaration(
    name="click",
    description="Tap a target.",
    parameters={"type": "object", "properties": {"target": {"type": "array"}}},
)
REPORT = ToolDeclaration(
    name="report_task_status",
    description="Finish.",
    parameters={"type": "object", "properties": {"status": {"type": "string"}}},
)
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()


def test_parse_reply_plain_and_fenced():
    plain = '{"thought": "Tap basket.", "tool_calls": [{"name": "click", "args": {"target": [500, 930]}}]}'
    assert parse_reply(plain) == ("Tap basket.", [{"name": "click", "args": {"target": [500, 930]}}])
    fenced = "Sure:\n```json\n" + plain + "\n```"
    assert parse_reply(fenced)[1][0]["name"] == "click"


def test_parse_reply_without_json_keeps_text_and_no_calls():
    assert parse_reply("I cannot see the screen.") == ("I cannot see the screen.", [])


def test_factory_builds_agy_model():
    assert ModelProvider.from_string("antigravity") is ModelProvider.AGY
    model = ModelFactory.create_model(ModelEndpoint(provider="agy", model_name="gemini-3.8-flash-low"))
    assert isinstance(model, AgyChatModel)
    assert model.model_name == "gemini-3.8-flash-low"


@pytest.fixture
def session(tmp_path):
    s = agy_model._AgySession("agy", "m", 10)
    s.workdir = str(tmp_path)
    s.proc = SimpleNamespace(returncode=None)  # alive
    return s


def test_first_turn_sends_protocol_tools_system_and_screenshot_file(session):
    model = AgyChatModel().bind_tools([CLICK, REPORT])
    messages = [
        SystemMessage(content="Objective: open the basket."),
        HumanMessage(content=[{"type": "text", "text": "# CURRENT OBSERVATION"}, {"type": "image_url", "image_url": {"url": PNG}}]),
    ]
    turn = model._build_turn(session, messages)

    assert turn.startswith(PROTOCOL_HEADER)
    assert '"name": "click"' in turn and '"name": "report_task_status"' in turn
    assert "Objective: open the basket." in turn
    shot = os.path.join(session.workdir, "screen_0001.png")
    assert f"[Screenshot image file: {shot}" in turn
    assert open(shot, "rb").read() == b"\x89PNG fake"


def test_later_turn_sends_only_what_follows_the_last_reply(session):
    model = AgyChatModel().bind_tools([CLICK, REPORT])
    system = SystemMessage(content="Objective: open the basket.")
    first = HumanMessage(content="obs 1")
    model._build_turn(session, [system, first])

    reply = AIMessage(id="agy-r1", content="Tap.", tool_calls=[{"name": "click", "args": {}, "id": "c1"}])
    session.reply_ids.add("agy-r1")
    messages = [
        system,
        first,
        reply,
        ToolMessage(content="Tapped at [500, 931].", tool_call_id="c1", name="click"),
        HumanMessage(content=[{"type": "text", "text": "obs 2"}, {"type": "image_url", "image_url": {"url": PNG}}]),
    ]
    turn = model._build_turn(session, messages)

    assert "Objective" not in turn and "obs 1" not in turn and PROTOCOL_HEADER not in turn
    assert "# TOOL RESULT (click)\nTapped at [500, 931]." in turn
    assert "obs 2" in turn and "screen_0001.png" in turn


def test_restricted_tool_set_is_announced(session):
    system = SystemMessage(content="S")
    AgyChatModel().bind_tools([CLICK, REPORT])._build_turn(session, [system, HumanMessage(content="o1")])
    session.reply_ids.add("r1")
    final_turn = AgyChatModel().bind_tools([REPORT])._build_turn(
        session, [system, HumanMessage(content="o1"), AIMessage(id="r1", content="x"), HumanMessage(content="o2")]
    )
    assert "Only these device tools may be called now: report_task_status" in final_turn


def test_bound_copies_share_open_conversations():
    base = AgyChatModel()
    a = base.bind_tools([CLICK])
    b = a.bind_tools([REPORT])
    assert a._sessions is base._sessions is b._sessions


@pytest.mark.asyncio
async def test_agenerate_parses_reply_into_tool_calls(monkeypatch, tmp_path):
    model = AgyChatModel().bind_tools([CLICK])
    sent = []

    async def fake_start(self):
        self.proc = SimpleNamespace(returncode=None)
        self.workdir = str(tmp_path)

    async def fake_ask(self, text):
        sent.append(text)
        return {
            "status": "SUCCESS",
            "response": json.dumps({"thought": "Basket bar.", "tool_calls": [{"name": "click", "args": {"target": [500, 929]}}]}),
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        }

    monkeypatch.setattr(agy_model._AgySession, "_start", fake_start)
    monkeypatch.setattr(agy_model._AgySession, "ask", fake_ask)
    result = await model.ainvoke([SystemMessage(content="S"), HumanMessage(content="o1")])

    assert result.content == "Basket bar."
    assert result.tool_calls[0]["name"] == "click"
    assert result.tool_calls[0]["args"] == {"target": [500, 929]}
    assert result.usage_metadata["total_tokens"] == 120
    session = next(iter(model._sessions.values()))
    assert result.id in session.reply_ids


@pytest.mark.asyncio
async def test_agenerate_recycles_session_when_threshold_exceeded(monkeypatch, tmp_path):
    model = AgyChatModel(max_context_tokens=500).bind_tools([CLICK])
    closed = []

    async def fake_start(self):
        self.proc = SimpleNamespace(returncode=None)
        self.workdir = str(tmp_path)

    async def fake_ask(self, text):
        return {
            "status": "SUCCESS",
            "response": json.dumps({"thought": "Done.", "tool_calls": []}),
            "usage": {"input_tokens": 800, "output_tokens": 10, "total_tokens": 810},
        }

    async def fake_close(self, keep_files=False):
        closed.append(True)

    monkeypatch.setattr(agy_model._AgySession, "_start", fake_start)
    monkeypatch.setattr(agy_model._AgySession, "ask", fake_ask)
    monkeypatch.setattr(agy_model._AgySession, "close", fake_close)

    await model.ainvoke([SystemMessage(content="S2"), HumanMessage(content="o1")])
    assert len(closed) == 1
    # The session key should have been evicted from model._sessions
    assert len(model._sessions) == 0


@pytest.mark.asyncio
async def test_agenerate_recycles_session_on_delta_threshold(monkeypatch, tmp_path):
    model = AgyChatModel(max_context_tokens=750, context_delta_tokens=150).bind_tools([CLICK])
    closed = []
    tokens_sequence = [600, 700, 800]
    call_idx = 0

    async def fake_start(self):
        self.proc = SimpleNamespace(returncode=None)
        self.workdir = str(tmp_path)

    async def fake_ask(self, text):
        nonlocal call_idx
        inp = tokens_sequence[min(call_idx, len(tokens_sequence) - 1)]
        call_idx += 1
        return {
            "status": "SUCCESS",
            "response": json.dumps({"thought": f"Step {call_idx}", "tool_calls": []}),
            "usage": {"input_tokens": inp, "output_tokens": 10, "total_tokens": inp + 10},
        }

    async def fake_close(self, keep_files=False):
        closed.append(True)

    monkeypatch.setattr(agy_model._AgySession, "_start", fake_start)
    monkeypatch.setattr(agy_model._AgySession, "ask", fake_ask)
    monkeypatch.setattr(agy_model._AgySession, "close", fake_close)

    # Turn 1: initial tokens = 600, delta = 0 -> should NOT recycle
    await model.ainvoke([SystemMessage(content="S2"), HumanMessage(content="turn 1")])
    assert len(closed) == 0
    assert len(model._sessions) == 1

    # Turn 2: tokens = 700, delta = 100 < 150 -> should NOT recycle
    await model.ainvoke([SystemMessage(content="S2"), HumanMessage(content="turn 2")])
    assert len(closed) == 0
    assert len(model._sessions) == 1

    # Turn 3: tokens = 800, delta = 200 >= 150 -> SHOULD recycle!
    await model.ainvoke([SystemMessage(content="S2"), HumanMessage(content="turn 3")])
    assert len(closed) == 1
    assert len(model._sessions) == 0

