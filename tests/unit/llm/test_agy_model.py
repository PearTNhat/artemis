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
    model = AgyChatModel(transport="stream-json").bind_tools([CLICK, REPORT])
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
    model = AgyChatModel(transport="stream-json").bind_tools([CLICK, REPORT])
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
    AgyChatModel(transport="stream-json").bind_tools([CLICK, REPORT])._build_turn(session, [system, HumanMessage(content="o1")])
    session.reply_ids.add("r1")
    final_turn = AgyChatModel(transport="stream-json").bind_tools([REPORT])._build_turn(
        session, [system, HumanMessage(content="o1"), AIMessage(id="r1", content="x"), HumanMessage(content="o2")]
    )
    assert "Only these device tools may be called now: report_task_status" in final_turn


def test_bound_copies_share_open_conversations():
    base = AgyChatModel(transport="stream-json")
    a = base.bind_tools([CLICK])
    b = a.bind_tools([REPORT])
    assert a._sessions is base._sessions is b._sessions


@pytest.mark.asyncio
async def test_agenerate_parses_reply_into_tool_calls(monkeypatch, tmp_path):
    model = AgyChatModel(transport="stream-json").bind_tools([CLICK])
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
    model = AgyChatModel(transport="stream-json", max_context_tokens=500).bind_tools([CLICK])
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
    model = AgyChatModel(transport="stream-json", max_context_tokens=750, context_delta_tokens=150).bind_tools([CLICK])
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



# -- ACP transport (`agy acp`): screenshots inline, one model call per turn -------

FAKE_ACP = r'''#!__PY__
import json, sys
image_cap = __IMAGE_CAP__
log = open(__LOG__, "a")
def send(m):
    sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
for raw in sys.stdin:
    m = json.loads(raw)
    log.write(json.dumps(m) + "\n"); log.flush()
    method = m.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": 1,
              "agentCapabilities": {"promptCapabilities": {"image": image_cap}}}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"sessionId": "s1",
              "models": {"availableModels": [{"modelId": "m"}], "currentModelId": "other"}}})
    elif method == "session/set_model":
        send({"jsonrpc": "2.0", "id": m["id"], "result": {}})
    elif method == "session/prompt":
        images = [b for b in m["params"]["prompt"] if b.get("type") == "image"]
        # The agent asks for a permission: the client must refuse it.
        send({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {}})
        reply = {"thought": f"saw {len(images)} image(s)", "tool_calls": [{"name": "click", "args": {"target": [1, 2]}}]}
        text = json.dumps(reply)
        for chunk in (text[:10], text[10:]):
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1",
                  "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": chunk}}}})
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"stopReason": "end_turn",
              "usage": {"inputTokens": 20, "cachedReadTokens": 100, "outputTokens": 30, "totalTokens": 150}}})
'''


def _fake_agy(tmp_path, image_cap=True):
    import sys as _sys

    log = tmp_path / "acp.log"
    script = tmp_path / "fake-agy"
    script.write_text(
        FAKE_ACP.replace("__PY__", _sys.executable)
        .replace("__IMAGE_CAP__", "True" if image_cap else "False")
        .replace("__LOG__", repr(str(log)))
    )
    script.chmod(0o755)
    return str(script), log


def _logged(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.mark.asyncio
async def test_acp_turn_sends_screenshot_inline_and_parses_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(agy_model, "_ACP_UNAVAILABLE", False)
    agy_bin, log = _fake_agy(tmp_path)
    model = AgyChatModel(agy_bin=agy_bin, model_name="m", transport="acp").bind_tools([CLICK])
    observation = HumanMessage(
        content=[{"type": "text", "text": "o1"}, {"type": "image_url", "image_url": {"url": PNG}}]
    )
    try:
        result = await model.ainvoke([SystemMessage(content="S-acp"), observation])
    finally:
        for s in list(model._sessions.values()):
            await s.close()

    assert result.content == "saw 1 image(s)"
    assert result.tool_calls[0]["name"] == "click"
    # Cached prefix tokens count toward the context size (recycle gate).
    assert result.usage_metadata["input_tokens"] == 120
    assert result.response_metadata["agy"]["transport"] == "acp"
    assert result.response_metadata["agy"]["model_calls"] == 1

    sent = _logged(log)
    assert [m.get("method") for m in sent if m.get("method")] == [
        "initialize", "session/new", "session/set_model", "session/prompt",
    ]
    prompt = next(m for m in sent if m.get("method") == "session/prompt")["params"]["prompt"]
    image = next(b for b in prompt if b["type"] == "image")
    assert image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"]) == b"\x89PNG fake"
    text = next(b for b in prompt if b["type"] == "text")["text"]
    assert "attached as an image" in text and "read_file" not in text
    # The permission request was refused, never granted.
    refusal = next(m for m in sent if m.get("id") == 900)
    assert refusal["result"]["outcome"]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_acp_without_image_support_falls_back_to_stream_json(tmp_path, monkeypatch):
    monkeypatch.setattr(agy_model, "_ACP_UNAVAILABLE", False)
    agy_bin, _ = _fake_agy(tmp_path, image_cap=False)

    started = []

    async def fake_stream_start(self):
        started.append(type(self).__name__)
        self.proc = SimpleNamespace(returncode=None)
        self.workdir = str(tmp_path)

    async def fake_stream_ask(self, text):
        assert "read_file" in text  # stream-json protocol
        return {"status": "SUCCESS", "response": json.dumps({"thought": "ok", "tool_calls": []}), "usage": {}}

    monkeypatch.setattr(agy_model._AgySession, "_start", fake_stream_start)
    monkeypatch.setattr(agy_model._AgySession, "ask", fake_stream_ask)
    monkeypatch.setattr(agy_model._AgySession, "close", lambda self, keep_files=False: _noop())
    model = AgyChatModel(agy_bin=agy_bin, transport="acp").bind_tools([CLICK])

    result = await model.ainvoke([SystemMessage(content="S-fallback"), HumanMessage(content="o1")])

    assert result.content == "ok"
    assert started == ["_AgySession"]
    assert agy_model._ACP_UNAVAILABLE is True
    assert type(next(iter(model._sessions.values()))).__name__ == "_AgySession"


async def _noop():
    return None
