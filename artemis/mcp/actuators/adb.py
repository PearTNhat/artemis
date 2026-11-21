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

"""ADB-backed actuator: the reference implementation of the actuator contract.

Outcome messages describe the dispatched command, such as
"Tapped at [x, y] (normalized)." The agent verifies the effect from subsequent
observations.

Error convention: device-level refusals the controller *reports* (an ``error`` field,
a falsy success flag) come back as ``ActionResult(ok=False)`` with the historical
message. Unexpected exceptions are allowed to propagate -- the caller (executor or MCP
server layer) owns the ``"Error during X: {e}"`` wrapping, exactly as the original
executor's ``try/except`` did.
"""

import asyncio
import base64
import inspect
import io
import json
import re
import time
from typing import Any

from PIL import Image

from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.mcp.action_manifest import DEVICE_ACTIONS, ExtensionTool
from artemis.mcp.action_types import ActionCode, ActionResult
from artemis.utils.screen_diff import (
    compare_screen_pixels,
    compare_xml_hierarchy,
    find_matching_text_elements,
    mask_noise,
)
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["AdbActuator", "find_element_at_coords", "ensure_focus_at_coords"]


def find_element_at_coords(elements: list[dict], x: int, y: int) -> dict | None:
    """Finds the smallest (leaf-most) focusable element containing pixel [x, y].

    Moved from ``artemis.mcp.adb_server`` so both the stdio server and in-process
    actuator share one implementation.
    """
    matching_element = None
    min_area = float("inf")

    for elem in elements:
        is_focusable = (
            elem.get("focusable") == "true"
            or elem.get("clickable") == "true"
            or "EditText" in str(elem.get("class", ""))
        )
        if not is_focusable:
            continue

        bounds_str = elem.get("bounds")
        if bounds_str and isinstance(bounds_str, str):
            match = re.match(r"\[(\-?\d+),(\-?\d+)\]\[(\-?\d+),(\-?\d+)\]", bounds_str)
            if match:
                x1, y1, x2, y2 = map(int, match.groups())
                if x1 <= x <= x2 and y1 <= y <= y2:
                    area = (x2 - x1) * (y2 - y1)
                    if area < min_area:
                        min_area = area
                        matching_element = elem

    return matching_element


async def ensure_focus_at_coords(controller, x: int, y: int) -> str | None:
    """Ensures the element at pixel [x, y] is focused, tapping only if needed.

    Returns an error string on failure, ``None`` on success. Moved from
    ``artemis.mcp.adb_server``.
    """
    try:
        elements = await controller.get_ui_elements()
        elem = find_element_at_coords(elements, x, y)
        if elem and elem.get("focused") == "true":
            logger.info(f"Element under [{x}, {y}] is already focused. Skipping tap.")
            return None
    except Exception as e:
        logger.warning(f"Failed to check focus status: {e}. Falling back to unconditional tap.")

    result = await controller.tap_at(x=x, y=y)
    if hasattr(result, "error") and result.error:
        return result.error

    # Wait until an editable field holds focus (the tap may also open a new
    # screen with its own search field), at most the old fixed 1.0 s.
    await _wait_for_focused_input(controller, timeout_s=1.0)
    return None


async def _wait_for_focused_input(controller, timeout_s: float = 1.0, poll_s: float = 0.15) -> bool:
    """True once a focused editable node appears; False after ``timeout_s``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    await asyncio.sleep(poll_s)
    while True:
        try:
            elements = await controller.get_ui_elements()
            if any(
                str(e.get("focused")).lower() == "true"
                and ("EditText" in str(e.get("class", "")) or str(e.get("editable")).lower() == "true")
                for e in elements or []
            ):
                return True
        except Exception as e:
            logger.debug(f"Focus poll failed: {e}")
            await asyncio.sleep(max(0.0, deadline - loop.time()))
            return False
        if loop.time() + poll_s > deadline:
            await asyncio.sleep(max(0.0, deadline - loop.time()))
            return False
        await asyncio.sleep(poll_s)


class AdbActuator:
    """Drives an Android device through ``UnifiedMobileController`` over ADB."""

    def __init__(
        self,
        ctx: ArtemisContext,
        controller: UnifiedMobileController | None = None,
    ):
        self.ctx = ctx
        self.controller = controller or UnifiedMobileController(ctx)
        self._wait_count: int = 0
        self._cumulative_wait_s: float = 0.0

    def _reset_wait_tracking(self) -> None:
        self._wait_count = 0
        self._cumulative_wait_s = 0.0

    # --- Contract --------------------------------------------------------------------

    def capabilities(self) -> frozenset[str]:
        return DEVICE_ACTIONS

    def extensions(self) -> list[ExtensionTool]:
        return []

    # --- Coordinate helpers ----------------------------------------------------------

    def _dims(self) -> tuple[int, int]:
        width = getattr(self.ctx.device, "device_width", 1080) if self.ctx.device else 1080
        height = getattr(self.ctx.device, "device_height", 2400) if self.ctx.device else 2400
        return width or 1080, height or 2400

    def _to_px(self, nx: int, ny: int) -> tuple[int, int]:
        width, height = self._dims()
        x = int(max(0, min(width - 1, int(nx) * width / 1000)))
        y = int(max(0, min(height - 1, int(ny) * height / 1000)))
        return x, y

    # --- Required action -------------------------------------------------------------

    async def click_sequence(
        self, points: list[tuple[int, int]], delay_ms: int = 50
    ) -> ActionResult:
        self._reset_wait_tracking()
        outcomes = []
        for i, (nx, ny) in enumerate(points):
            x, y = self._to_px(nx, ny)
            result = await self.controller.tap_at(x, y)
            err = getattr(result, "error", None) if result else None
            if err:
                return ActionResult.failure(
                    "click_sequence",
                    f"Error executing click at step {i + 1}: {err}",
                    detail=str(err),
                )
            outcomes.append(f"[{nx}, {ny}]")
            if i < len(points) - 1:
                await asyncio.sleep(max(0, delay_ms) / 1000.0)

        return ActionResult.success(
            "click_sequence",
            f"Tapped in sequence at {'; '.join(outcomes)} (normalized).",
        )

    # --- Optional actions ------------------------------------------------------------

    async def click(self, nx: int, ny: int, times: int = 1, delay_ms: int = 100) -> ActionResult:
        self._reset_wait_tracking()
        x, y = self._to_px(nx, ny)
        result = await self.controller.tap_at(x, y, times=times, delay_ms=delay_ms)
        err = getattr(result, "error", None) if result else None
        if err is not None:
            return ActionResult.failure("click", f"Error executing click: {err}", detail=str(err))
        return ActionResult.success(
            "click",
            f"Tapped at [{nx}, {ny}] (normalized).",
            normalized_coordinates=[int(nx), int(ny)],
        )

    async def long_press(self, nx: int, ny: int, duration_ms: int = 1000) -> ActionResult:
        self._reset_wait_tracking()
        x, y = self._to_px(nx, ny)
        result = await self.controller.tap_at(
            x, y, long_press=True, long_press_duration=duration_ms
        )
        err = getattr(result, "error", None) if result else None
        if err is not None:
            return ActionResult.failure(
                "long_press", f"Error executing long press: {err}", detail=str(err)
            )
        return ActionResult.success(
            "long_press",
            f"Long-pressed at [{nx}, {ny}] (normalized) for {duration_ms}ms.",
            normalized_coordinates=[int(nx), int(ny)],
            duration_ms=duration_ms,
        )

    async def input_text(
        self,
        text: str,
        target: tuple[int, int] | None = None,
        clear_exist: bool = True,
    ) -> ActionResult:
        self._reset_wait_tracking()
        coords = None
        if target:
            nx, ny = target
            x, y = self._to_px(nx, ny)
            coords = [int(nx), int(ny)]
            err = await ensure_focus_at_coords(self.controller, x, y)
            if err:
                return ActionResult.failure(
                    "input_text",
                    f"Error focusing element: {err}",
                    detail=str(err),
                    normalized_coordinates=coords,
                )
        if not hasattr(self.controller, "type_text"):
            if clear_exist:
                if hasattr(self.controller, "erase_text") and not await self.controller.erase_text():
                    return ActionResult.failure(
                        "input_text",
                        "Failed to clear existing text",
                        normalized_coordinates=coords,
                    )
            elif hasattr(self.controller, "press_key"):
                # KEYCODE_MOVE_END: move cursor to the end for reliable append.
                await self.controller.press_key("123")
        # The driver clears (one helper RPC, key events only as a fallback) and,
        # in append mode, moves the caret to the end itself.
        elif not await self.controller.type_text(text, clear_existing=clear_exist):
            return ActionResult.failure(
                "input_text",
                f"Failed typing '{text}'.",
                normalized_coordinates=coords,
            )

        return ActionResult.success(
            "input_text",
            f"Typed '{text}'"
            + (f" at [{coords[0]}, {coords[1]}] (normalized)." if coords else "."),
            normalized_coordinates=coords,
        )

    async def swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_ms: int = 800,
    ) -> ActionResult:
        self._reset_wait_tracking()
        nx1, ny1 = start
        nx2, ny2 = end
        px1, py1 = self._to_px(nx1, ny1)
        px2, py2 = self._to_px(nx2, ny2)
        err = await self.controller.swipe_coords(px1, py1, px2, py2, duration_ms)
        if err:
            return ActionResult.failure("swipe", f"Error dragging: {err}", detail=str(err))
        return ActionResult.success(
            "swipe",
            f"Swiped from [{nx1}, {ny1}] to [{nx2}, {ny2}] (normalized).",
            normalized_coordinates=[int(nx1), int(ny1), int(nx2), int(ny2)],
            duration_ms=duration_ms,
        )

    async def press_key(self, key: str) -> ActionResult:
        self._reset_wait_tracking()
        key_str = str(key).lower()
        if not key_str:
            return ActionResult.failure(
                "press_key",
                f"Error executing key press '{key}'.",
                code=ActionCode.INVALID_ARGS,
            )

        if key_str == "back" and hasattr(self.controller, "go_back"):
            await self.controller.go_back()
        elif key_str == "home" and hasattr(self.controller, "go_home"):
            await self.controller.go_home()
        elif key_str == "enter" and hasattr(self.controller, "press_enter"):
            await self.controller.press_enter()
        elif hasattr(self.controller, "press_key"):
            # Pass unrecognized keycodes through verbatim (KEYCODE_* names, numeric
            # codes): the driver resolves or forwards them, matching the historical
            # adb_server behavior of accepting any Android key event.
            res = await self.controller.press_key(key)
            err = getattr(res, "error", None) if res else None
            if err:
                return ActionResult.failure(
                    "press_key",
                    f"Error executing key press '{key}': {err}",
                    detail=str(err),
                )
            if not res:
                return ActionResult.failure("press_key", f"Error executing key press '{key}'.")

        return ActionResult.success("press_key", f"Pressed key '{key}'.")

    async def manage_app(self, action: str, app_name: str) -> ActionResult:
        self._reset_wait_tracking()
        # Imported lazily: launch_app pulls in tool wrappers that are costly at import.
        from artemis.tools.mobile.launch_app import find_package
        from third_party.mobile_use.utils.app_launch_utils import launch_app_with_retries

        res = find_package(self.ctx, app_name, use_fallback=False)
        pkg = await res if inspect.iscoroutine(res) else res
        if not pkg:
            return ActionResult.failure(
                "manage_app",
                f"Error finding package for app: {app_name}",
                code=ActionCode.PACKAGE_NOT_FOUND,
            )
        target_pkg = pkg

        if action.lower() == "launch":
            res_launch = launch_app_with_retries(self.ctx, target_pkg)
            if inspect.iscoroutine(res_launch):
                res_launch = await res_launch
            if isinstance(res_launch, tuple):
                success, error_msg = res_launch
            else:
                success, error_msg = bool(res_launch), ""

            if success:
                return ActionResult.success(
                    "manage_app",
                    f"Launched app '{app_name}' ({target_pkg}); foreground confirmed.",
                )
            return ActionResult.failure(
                "manage_app",
                f"Failed to launch app '{app_name}': {error_msg}",
                detail=error_msg or None,
            )
        elif action.lower() == "stop":
            if hasattr(self.controller, "terminate_app"):
                res_term = self.controller.terminate_app(target_pkg)
                if inspect.iscoroutine(res_term):
                    await res_term
            return ActionResult.success(
                "manage_app", f"Force-stopped app '{app_name}' ({target_pkg})."
            )
        return ActionResult.failure(
            "manage_app",
            f"Invalid manage_app action: {action}",
            code=ActionCode.INVALID_ARGS,
        )

    async def wait_for_delay(self, time_in_ms: int) -> ActionResult:
        delay_s = max(0.0, float(time_in_ms) / 1000.0)
        await asyncio.sleep(delay_s)
        return ActionResult.success(
            "wait_for_delay",
            f"Waited {time_in_ms}ms.",
            duration_ms=int(time_in_ms),
        )

    async def wait_for_screen_change(
        self,
        timeout_ms: int = 25000,
        region: list[int] | None = None,
        after: str = "my_action",
    ) -> ActionResult:
        timeout_s = max(1.0, float(timeout_ms) / 1000.0)
        start_time = time.monotonic()
        width, height = self._dims()

        # 1. Stabilization: Ensure previous transition/animation has settled before establishing baseline
        # Compares noise-masked downscaled images so ticking clocks or blinking cursors don't block stabilization.
        base_img = None
        base_elements = None
        try:
            prev_data = await self.controller.get_screen_data()
            prev_bytes = base64.b64decode(prev_data.base64) if getattr(prev_data, "base64", None) else None
            width = getattr(prev_data, "width", None) or width
            height = getattr(prev_data, "height", None) or height
            prev_elements = getattr(prev_data, "elements", None) or []
            prev_masked = None
            if prev_bytes:
                prev_raw = Image.open(io.BytesIO(prev_bytes))
                prev_masked = mask_noise(prev_raw, prev_elements, width, height)

            stabilize_deadline = time.monotonic() + 1.5
            curr_bytes = prev_bytes
            curr_data = prev_data
            curr_masked = prev_masked

            while time.monotonic() < stabilize_deadline:
                await asyncio.sleep(0.3)
                curr_data = await self.controller.get_screen_data()
                curr_bytes = base64.b64decode(curr_data.base64) if getattr(curr_data, "base64", None) else None
                curr_elements = getattr(curr_data, "elements", None) or []
                if curr_bytes:
                    curr_raw = Image.open(io.BytesIO(curr_bytes))
                    curr_masked = mask_noise(curr_raw, curr_elements, width, height)
                    if prev_masked is not None:
                        is_changed, _ = compare_screen_pixels(
                            prev_masked, curr_masked, region=region, width=width, height=height
                        )
                        if not is_changed:
                            # Visual settling confirmed
                            break
                    prev_masked = curr_masked

            base_elements = getattr(curr_data, "elements", None) or []
            base_img = curr_masked
        except Exception as e:
            logger.warning(f"Failed to capture baseline for wait_for_screen_change: {e}")

        # 2. Polling loop
        while time.monotonic() - start_time < timeout_s:
            await asyncio.sleep(0.7)
            try:
                curr_data = await self.controller.get_screen_data()
                curr_elements = getattr(curr_data, "elements", None) or []
                curr_bytes = base64.b64decode(curr_data.base64) if getattr(curr_data, "base64", None) else None

                # Check XML hierarchy diff (excluding clock elements, scoped to region if specified)
                if base_elements is not None and curr_elements:
                    xml_changed, xml_diff = compare_xml_hierarchy(
                        base_elements, curr_elements, region=region, width=width, height=height
                    )
                    if xml_changed:
                        elapsed = round(time.monotonic() - start_time, 1)
                        self._reset_wait_tracking()
                        return ActionResult.success(
                            "wait_for_screen_change",
                            f"Screen changed after {elapsed}s ({xml_diff}).",
                            duration_ms=int((time.monotonic() - start_time) * 1000),
                        )

                # Check pixel diff with noise masking
                if base_img is not None and curr_bytes:
                    curr_raw = Image.open(io.BytesIO(curr_bytes))
                    curr_masked = mask_noise(curr_raw, curr_elements, width, height)
                    pix_changed, changed_cells = compare_screen_pixels(
                        base_img, curr_masked, region=region, width=width, height=height
                    )
                    if pix_changed:
                        elapsed = round(time.monotonic() - start_time, 1)
                        self._reset_wait_tracking()
                        return ActionResult.success(
                            "wait_for_screen_change",
                            f"Screen visually changed after {elapsed}s ({changed_cells} cells changed).",
                            duration_ms=int((time.monotonic() - start_time) * 1000),
                        )
            except Exception as e:
                logger.debug(f"Error during wait_for_screen_change polling frame: {e}")

        elapsed = round(time.monotonic() - start_time, 1)
        self._wait_count += 1
        self._cumulative_wait_s += elapsed
        msg = (
            f"[TIMEOUT_REACHED] Đã chờ {elapsed}s chưa thấy màn hình thay đổi "
            f"(lần chờ thứ {self._wait_count}, tổng {round(self._cumulative_wait_s, 1)}s). "
            f"Màn hình hiện tại được đính kèm để bạn đánh giá tiếp tục chờ hay thao tác."
        )
        return ActionResult.success(
            "wait_for_screen_change",
            msg,
            timed_out=True,
            duration_ms=int((time.monotonic() - start_time) * 1000),
        )

    async def wait_for_text(
        self, text: str | list[str], wait_state: str | None = None, timeout_ms: int | None = None
    ) -> ActionResult:
        timeout_s = max(1.0, float(timeout_ms or 8000) / 1000.0)
        start_time = time.monotonic()
        target_state = (wait_state or "appear").lower()
        width, height = self._dims()

        # Parse target text candidates (supports str, JSON-encoded list, or list of str)
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
                targets = parsed if isinstance(parsed, list) else [text]
            except Exception:
                targets = [text]
        else:
            targets = list(text)

        consecutive_gone = 0
        while time.monotonic() - start_time < timeout_s:
            try:
                curr_data = await self.controller.get_screen_data()
                curr_elements = getattr(curr_data, "elements", None) or []
                width = getattr(curr_data, "width", None) or width
                height = getattr(curr_data, "height", None) or height

                matches = find_matching_text_elements(curr_elements, targets, width=width, height=height)

                if target_state == "appear":
                    if matches:
                        matched_el = matches[0]
                        matched_text = matched_el.get("text") or matched_el.get("_matched_target", targets[0])
                        matched_bounds = matched_el.get("_parsed_bounds", matched_el.get("bounds"))
                        elapsed = round(time.monotonic() - start_time, 1)
                        self._reset_wait_tracking()
                        return ActionResult.success(
                            "wait_for_text",
                            f"Found text '{matched_text}' after {elapsed}s at bounds {matched_bounds}.",
                            duration_ms=int((time.monotonic() - start_time) * 1000),
                        )
                elif target_state == "disappear":
                    # Bug d: Do not conclude gone if XML element list is empty (dump glitch),
                    # and require 2 consecutive confirmations.
                    if not curr_elements:
                        consecutive_gone = 0
                    elif matches:
                        consecutive_gone = 0
                    else:
                        consecutive_gone += 1
                        if consecutive_gone >= 2:
                            elapsed = round(time.monotonic() - start_time, 1)
                            self._reset_wait_tracking()
                            return ActionResult.success(
                                "wait_until_gone",
                                f"Text '{targets}' disappeared from screen after {elapsed}s.",
                                duration_ms=int((time.monotonic() - start_time) * 1000),
                            )
            except Exception as e:
                logger.debug(f"Error during wait_for_text polling frame: {e}")

            await asyncio.sleep(0.5)

        elapsed = round(time.monotonic() - start_time, 1)
        self._wait_count += 1
        self._cumulative_wait_s += elapsed
        action_name = "wait_until_gone" if target_state == "disappear" else "wait_for_text"
        state_desc = "chưa thấy biến mất text" if target_state == "disappear" else "chưa thấy xuất hiện text"
        msg = (
            f"[TIMEOUT_REACHED] Đã chờ {elapsed}s {state_desc} '{targets}' "
            f"(lần chờ thứ {self._wait_count}, tổng {round(self._cumulative_wait_s, 1)}s). "
            f"Màn hình hiện tại được đính kèm để bạn đánh giá tiếp tục chờ hay thao tác."
        )
        return ActionResult.success(
            action_name,
            msg,
            timed_out=True,
            duration_ms=int((time.monotonic() - start_time) * 1000),
        )

    async def wait_until_gone(
        self, text: str | list[str], timeout_ms: int = 15000
    ) -> ActionResult:
        return await self.wait_for_text(text, wait_state="disappear", timeout_ms=timeout_ms)

    async def open_link(self, url: str) -> ActionResult:
        self._reset_wait_tracking()
        success = await self.controller.open_url(url)
        if success:
            return ActionResult.success("open_link", f"Opened link '{url}'.")
        return ActionResult.failure("open_link", f"Failed to open link '{url}'.")

    async def erase_one_char(self) -> ActionResult:
        self._reset_wait_tracking()
        success = await self.controller.erase_text(nb_chars=1)
        if success:
            return ActionResult.success("erase_one_char", "Erased one character.")
        return ActionResult.failure("erase_one_char", "Failed to erase one character.")

    async def focus_and_clear_text(self, nx: int, ny: int) -> ActionResult:
        self._reset_wait_tracking()
        x, y = self._to_px(nx, ny)
        err = await ensure_focus_at_coords(self.controller, x, y)
        if err:
            return ActionResult.failure(
                "focus_and_clear_text",
                f"Error focusing element: {err}",
                detail=str(err),
                normalized_coordinates=[int(nx), int(ny)],
            )
        success = await self.controller.erase_text()
        if success:
            return ActionResult.success(
                "focus_and_clear_text",
                f"Cleared text at [{nx}, {ny}] (normalized).",
                normalized_coordinates=[int(nx), int(ny)],
            )
        return ActionResult.failure("focus_and_clear_text", "Failed to erase text.")

    # --- Internal observation primitives ---------------------------------------------

    async def take_screenshot(self) -> str:
        return await self.controller.take_screenshot()

    async def get_ui_elements(self) -> Any:
        return await self.controller.get_ui_elements()

    async def get_screen_data(self) -> Any:
        return await self.controller.get_screen_data()
