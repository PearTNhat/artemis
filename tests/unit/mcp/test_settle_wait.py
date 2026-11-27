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

"""Flash waits for the UI to stop moving before observing (no fixed delay)."""

from __future__ import annotations

import asyncio
import base64
import io

from PIL import Image

from artemis.mcp.action_executor import McpActionExecutor
from artemis.utils.image_hash import dhash_hex


def _png(kind: str) -> bytes:
    img = Image.new("L", (90, 160))
    px = img.load()
    for x in range(90):
        for y in range(160):
            if kind == "old":
                px[x, y] = (x * 3) % 256
            elif kind == "mid":
                px[x, y] = ((x * 3) + (y * 2)) % 256
            else:  # new
                px[x, y] = (255 - y) % 256
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _Frames:
    """Fake action session: returns the scripted frames, then repeats the last."""

    def __init__(self, kinds):
        self.kinds = list(kinds)
        self.calls = 0

    async def screenshot_b64(self, timeout=None):
        kind = self.kinds[min(self.calls, len(self.kinds) - 1)]
        self.calls += 1
        return base64.b64encode(_png(kind)).decode()


def _executor(agent="flash"):
    ex = McpActionExecutor.__new__(McpActionExecutor)
    ex.agent_name = agent
    ex._last_image_hash = dhash_hex(_png("old"))
    return ex


def test_tap_waits_for_the_transition_to_start_and_finish():
    session = _Frames(["old", "old", "mid", "new", "new"])
    res = asyncio.run(_executor()._wait_screen_settled(session, "click", {}, 400))
    assert res["changed"] and res["stable"]
    assert session.calls == 5  # did not stop on the still OLD frames, nor on the mid frame


def test_unchanged_tap_gives_up_after_the_change_window(monkeypatch):
    monkeypatch.setattr(McpActionExecutor, "_SETTLE_CHANGE_WINDOW_S", 0.05)
    session = _Frames(["old"])
    res = asyncio.run(_executor()._wait_screen_settled(session, "click", {}, 400))
    assert not res["changed"] and res["stable"] and res["expects_change"]


def test_typing_only_waits_for_stillness():
    session = _Frames(["old", "old"])
    res = asyncio.run(_executor()._wait_screen_settled(session, "input_text", {"text": "x"}, 400))
    assert res["stable"] and not res["expects_change"] and session.calls == 2


def test_never_still_screen_is_capped(monkeypatch):
    monkeypatch.setattr(McpActionExecutor, "_SETTLE_CAP_S", 0.2)
    flip = ["mid", "new"] * 200

    session = _Frames(flip)
    res = asyncio.run(_executor()._wait_screen_settled(session, "swipe", {}, 400))
    assert res["stable"] is False and res["settle_s"] < 1.0


def test_pro_and_sessions_without_screenshots_keep_the_fixed_delay():
    assert asyncio.run(_executor("validator")._wait_screen_settled(_Frames(["old"]), "click", {}, 400)) is None

    class _NoShot:
        pass

    assert asyncio.run(_executor()._wait_screen_settled(_NoShot(), "click", {}, 400)) is None
