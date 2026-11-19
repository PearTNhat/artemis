"""wait_for_screen_change(after=...) answered by the shared action server.

The server sits under both Flash and Pro, so these tests drive it through its MCP
tools exactly as the agents do: a screen is shown to the agent (``observe_screen``
for Flash, ``take_screenshot`` for Pro), the agent acts, then it waits.
Regression traces: Grab d912751d (waits timing out after the agent's own
navigation), Shopee 7a16cfb5 (turn-opening wait after the page had loaded), chess
(an illegal move waited 60 s for a change that never came).
"""

import base64
import io
import json

import pytest
from PIL import Image, ImageDraw

from artemis.mcp.action_server import build_action_server
from artemis.mcp.actuators import MockActuator
from artemis.mcp.action_types import ActionResult
from artemis.mcp.screen_effect import ScreenEffectTracker

W, H = 1080, 2400


def _png(mark=None) -> bytes:
    img = Image.new("RGB", (W, H), "white")
    if mark:
        ImageDraw.Draw(img).rectangle(list(mark), fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


BLANK = _png()
MOVED = _png((200, 900, 600, 1500))


class _Data:
    def __init__(self, image: bytes):
        self.base64 = base64.b64encode(image).decode()
        self.width, self.height = W, H
        self.elements = []


@pytest.fixture(autouse=True)
def _instant(monkeypatch):
    monkeypatch.setattr(ScreenEffectTracker, "JUDGE_DELAYS_S", (0.0, 0.0))
    monkeypatch.setattr(ScreenEffectTracker, "SETTLE_POLL_S", 0.0)


def _server(frames: list[bytes], shown: bytes):
    """A server whose device shows ``shown`` when the agent looks, then ``frames``."""
    actuator = MockActuator(W, H)
    frame_iter = iter(frames)
    last = {"img": frames[-1] if frames else shown}

    async def get_screen_data():
        try:
            last["img"] = next(frame_iter)
        except StopIteration:
            pass
        return _Data(last["img"])

    async def take_screenshot():
        return base64.b64encode(shown).decode()

    classic_waits: list[dict] = []

    async def classic_wait(timeout_ms=25000, region=None, after="my_action"):
        classic_waits.append({"after": after, "region": region})
        return ActionResult.success("wait_for_screen_change", "classic wait ran")

    actuator.get_screen_data = get_screen_data
    actuator.take_screenshot = take_screenshot
    actuator.wait_for_screen_change = classic_wait
    return build_action_server(actuator), classic_waits


def _structured(result) -> dict:
    if isinstance(result, tuple):
        return result[1]
    if hasattr(result, "structuredContent"):
        return result.structuredContent
    return json.loads(result[0].text)


async def _look_act_wait(server, *, act: bool, after: str = "my_action"):
    await server.call_tool("take_screenshot", {})  # the agent is shown the screen
    if act:
        await server.call_tool("click", {"target": [500, 500]})
    return _structured(await server.call_tool("wait_for_screen_change", {"after": after}))


@pytest.mark.asyncio
async def test_action_with_no_effect_skips_the_wait():
    server, classic = _server([BLANK] * 4, shown=BLANK)
    res = await _look_act_wait(server, act=True)
    assert res["no_effect"] is True
    assert res["message"].startswith("[NO_EFFECT]")
    assert classic == []


@pytest.mark.asyncio
async def test_action_that_already_changed_the_screen_returns_once_settled():
    server, classic = _server([MOVED] * 8, shown=BLANK)
    res = await _look_act_wait(server, act=True)
    assert "has settled" in res["message"]
    assert res["no_effect"] is False
    assert classic == []


@pytest.mark.asyncio
async def test_screen_still_moving_is_reported_not_called_settled():
    frames = [_png((100 + 100 * i, 900, 200 + 100 * i, 1500)) for i in range(9)]
    server, classic = _server(frames, shown=BLANK)
    res = await _look_act_wait(server, act=True)
    assert "still moving" in res["message"]
    assert classic == []


@pytest.mark.asyncio
async def test_turn_opening_wait_returns_when_page_already_loaded():
    """Shopee: the agent saw a skeleton, then opened its turn with a wait."""
    server, classic = _server([MOVED] * 8, shown=BLANK)
    res = await _look_act_wait(server, act=False)
    assert "has settled" in res["message"]
    assert classic == []


@pytest.mark.asyncio
async def test_turn_opening_wait_on_unchanged_screen_really_waits():
    """Nothing acted and nothing changed: a genuine wait for loading, never 'no effect'."""
    server, classic = _server([BLANK] * 4, shown=BLANK)
    res = await _look_act_wait(server, act=False)
    assert res["message"] == "classic wait ran"
    assert len(classic) == 1


@pytest.mark.asyncio
async def test_external_wait_always_waits_for_the_other_party():
    """Chess / chat: my move changed the board, but I am waiting for the opponent."""
    server, classic = _server([MOVED] * 4, shown=BLANK)
    res = await _look_act_wait(server, act=True, after="external")
    assert res["message"] == "classic wait ran"
    assert classic == [{"after": "external", "region": None}]


@pytest.mark.asyncio
async def test_without_a_seen_screen_the_classic_wait_runs():
    server, classic = _server([MOVED] * 4, shown=BLANK)
    res = _structured(await server.call_tool("wait_for_screen_change", {}))
    assert res["message"] == "classic wait ran"
    assert len(classic) == 1


@pytest.mark.asyncio
async def test_wrapped_tools_keep_their_input_schema():
    """The tracker wrappers must not change what the agents are shown."""
    plain = MockActuator(W, H)
    server = build_action_server(plain)
    tools = {t.name: t for t in await server.list_tools()}
    assert set(tools["wait_for_screen_change"].inputSchema["properties"]) == {"timeout_ms", "region", "after"}
    assert "target" in tools["click"].inputSchema["properties"]
