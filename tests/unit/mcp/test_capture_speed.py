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

"""Device-side waits: one settle per observation, focus polled instead of a fixed sleep."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from artemis.mcp import observation
from artemis.mcp.actuators import adb as adb_actuator


class _Controller:
    def __init__(self, focused_after: int | None):
        self.calls = 0
        self.focused_after = focused_after
        self.skip_flags: list[bool] = []

    async def get_ui_elements(self):
        self.calls += 1
        focused = self.focused_after is not None and self.calls >= self.focused_after
        return [{"class": "android.widget.EditText", "focused": "true" if focused else "false"}]

    async def get_screen_data(self, skip_settling: bool = False):
        self.skip_flags.append(skip_settling)
        return "data"


@pytest.mark.asyncio
async def test_focus_wait_returns_as_soon_as_a_field_is_focused():
    ctrl = _Controller(focused_after=1)
    started = time.monotonic()
    assert await adb_actuator._wait_for_focused_input(ctrl, timeout_s=1.0, poll_s=0.05) is True
    assert time.monotonic() - started < 0.5


@pytest.mark.asyncio
async def test_focus_wait_keeps_the_old_upper_bound_when_nothing_is_focused():
    ctrl = _Controller(focused_after=None)
    started = time.monotonic()
    assert await adb_actuator._wait_for_focused_input(ctrl, timeout_s=0.4, poll_s=0.05) is False
    assert 0.35 <= time.monotonic() - started < 0.8


@pytest.mark.asyncio
async def test_observation_capture_skips_the_driver_settle():
    ctrl = _Controller(focused_after=None)
    assert await observation._capture(ctrl) == "data"
    assert ctrl.skip_flags == [True]


@pytest.mark.asyncio
async def test_observation_capture_works_with_controllers_without_the_flag():
    ctrl = MagicMock()
    ctrl.get_screen_data = AsyncMock(return_value="data")
    assert await observation._capture(ctrl) == "data"
