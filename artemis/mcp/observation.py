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

"""Screen observation: capture, fuse, index, and persist -- without LangGraph State.

This is the act-then-observe half that used to be welded into every executor action
(``capture_screenshot_and_parse_ui``). Split out so that actuation and observation are
separate calls: the Flash adapter observes after each action, while the
Validator -- which runs its own UI-change polling -- simply never calls it.
"""

import asyncio
import base64
import inspect
import time
from typing import Any

from artemis.clients.screen_client_factory import DeviceOfflineError
from artemis.config import get_temp_dir
from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.mcp.action_types import ActionCode, DEVICE_OFFLINE_PREFIX, ObserveResult
from artemis.utils.ocr_xml_fusion import fuse_ocr_with_xml
from artemis.utils.visualization import format_minimal_list_with_elements
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["observe"]



def has_large_scrollable(nodes, width, height, min_fraction: float = 0.25) -> bool:
    """True when the hierarchy holds a scrollable container covering at least
    ``min_fraction`` of the screen (a page whose content can move)."""
    import re

    screen = float((width or 1080) * (height or 2400))
    stack = list(nodes) if isinstance(nodes, list) else [nodes]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        if str(node.get("scrollable", "")).lower() == "true" or node.get("scrollable") is True:
            bounds = node.get("bounds")
            box = None
            if isinstance(bounds, str):
                m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds)
                box = tuple(map(int, m.groups())) if m else None
            elif isinstance(bounds, (list, tuple)) and len(bounds) == 4:
                box = tuple(int(v) for v in bounds)
            if box and (box[2] - box[0]) * (box[3] - box[1]) >= min_fraction * screen:
                return True
        children = node.get("children") or node.get("nodes") or []
        if isinstance(children, (list, dict)):
            stack.extend(children if isinstance(children, list) else [children])
    return False

async def _local_ocr(ctx: ArtemisContext, screenshot_bytes: bytes, xml_hierarchy) -> list:
    """Local ONNX OCR for this frame when the Flash OCR config asks for it, else []."""
    from artemis.utils.local_ocr import local_ocr_settings, recognize_screenshot, should_run_ocr

    try:
        cfg = local_ocr_settings(ctx)
        if not should_run_ocr(cfg, xml_hierarchy):
            return []
        return await asyncio.to_thread(recognize_screenshot, screenshot_bytes, xml_hierarchy, cfg)
    except Exception as e:
        logger.warning(f"Local OCR skipped for this observation: {e}")
        return []


async def _gap_ocr(ctx: ArtemisContext, screenshot_bytes: bytes, xml_hierarchy) -> list:
    """Local OCR for gap filling under an overlay, unless the Flash OCR mode is 'off'."""
    from artemis.utils.local_ocr import local_ocr_settings, recognize_screenshot

    try:
        cfg = local_ocr_settings(ctx)
        if getattr(cfg, "mode", "off") == "off":
            return []
        return await asyncio.to_thread(recognize_screenshot, screenshot_bytes, xml_hierarchy, cfg)
    except Exception as e:
        logger.warning(f"Gap OCR skipped for this observation: {e}")
        return []


async def _capture(controller: Any) -> Any:
    """Screen data without the driver's built-in settle delay when supported."""
    try:
        accepts = "skip_settling" in inspect.signature(controller.get_screen_data).parameters
    except (TypeError, ValueError):
        accepts = False
    if accepts:
        return await controller.get_screen_data(skip_settling=True)
    return await controller.get_screen_data()


async def observe(
    ctx: ArtemisContext,
    controller: UnifiedMobileController,
    settle_ms: int = 400,
    ocr_results: list | None = None,
    persist: bool = True,
) -> tuple[ObserveResult, bytes | None]:
    """Captures and indexes the current screen.

    Returns the structured observation plus the raw screenshot bytes (an in-process
    convenience; the serialized ``ObserveResult`` carries only the persisted path).

    Mirrors the historical ``capture_screenshot_and_parse_ui`` behavior exactly:
    a settling delay, screen capture, XML fusion and element indexing, persistence via
    the data engine with a temp-file fallback -- and the same failure semantics: a
    failed capture yields ``ok=False``; a failed hierarchy parse still returns the
    screenshot with ``hierarchy_ok=False``.
    """
    timings: dict[str, float] = {}
    phase_start = time.monotonic()
    try:
        if settle_ms > 0:
            logger.info(f"Delaying {settle_ms / 1000:.1f}s before capturing screen state...")
            await asyncio.sleep(settle_ms / 1000.0)
        else:
            logger.info("Retrieving screen data directly (skip settling)...")
        timings["settle"] = round(time.monotonic() - phase_start, 3)
        phase_start = time.monotonic()
        # settle_ms above is the only settle: the driver's own delay is skipped.
        device_data = await _capture(controller)
        timings["capture"] = round(time.monotonic() - phase_start, 3)
        screenshot_bytes = base64.b64decode(device_data.base64)
    except DeviceOfflineError as e:
        logger.error(f"Device disconnected during screen observation: {e}")
        return (
            ObserveResult(
                ok=False,
                code=ActionCode.DEVICE_ERROR,
                message=f"{DEVICE_OFFLINE_PREFIX} {e}",
                hierarchy_ok=False,
            ),
            None,
        )
    except Exception as e:
        logger.error(f"Failed to capture screen state: {e}")
        return (
            ObserveResult(
                ok=False,
                code=ActionCode.DEVICE_ERROR,
                message=f"Failed to capture screen state: {e}",
                hierarchy_ok=False,
            ),
            None,
        )

    xml_hierarchy = None
    ocr_results = ocr_results or []
    elements: list[dict] = []
    hierarchy_ok = True
    scrollable: bool | None = None
    width = height = None
    try:
        xml_hierarchy = device_data.elements
        width = device_data.width or 1080
        height = device_data.height or 2400
        if ctx.device:
            ctx.device.device_width = width
            ctx.device.device_height = height

        scrollable = has_large_scrollable(xml_hierarchy, width, height)
        xml_first = False
        if not ocr_results:
            phase_start = time.monotonic()
            ocr_results = await _local_ocr(ctx, screenshot_bytes, xml_hierarchy)
            timings["ocr"] = round(time.monotonic() - phase_start, 3)
            xml_first = bool(ocr_results)
        phase_start = time.monotonic()
        # Fuse into copies: the raw hierarchy is what gets persisted and diffed.
        source = (
            [dict(n) if isinstance(n, dict) else n for n in (xml_hierarchy or [])]
            if ocr_results
            else xml_hierarchy
        )
        fused_xml = fuse_ocr_with_xml(source, ocr_results, xml_first=xml_first)
        compact = getattr(ctx, "flash_compact_list", False) is True
        # A layer on top (bottom sheet, dialog) in the compact list: check with OCR
        # which nodes of the screen below it really covers, leave those out, and
        # add the layer's text the tree does not declare (pinned buttons).
        hidden: set[int] = set()
        gap_results: list = []
        if compact and not ocr_results and isinstance(fused_xml, list):
            from artemis.utils.visualization import find_overlay_hidden, verified_overlay_hidden

            candidates, _layers = find_overlay_hidden(fused_xml, width, height)
            if candidates:
                ocr_started = time.monotonic()
                gap_results = await _gap_ocr(ctx, screenshot_bytes, xml_hierarchy)
                hidden = verified_overlay_hidden(fused_xml, gap_results, width, height)
                timings["ocr"] = round(time.monotonic() - ocr_started, 3)
        minimal_list_str, elements, _ = format_minimal_list_with_elements(
            fused_xml, width, height, compact=compact, hidden=hidden or None
        )
        if hidden and gap_results:
            from artemis.utils.visualization import gap_fill_ocr

            gap_lines, gap_elements = gap_fill_ocr(gap_results, elements, width, height)
            if gap_lines:
                minimal_list_str += "\n" + "\n".join(gap_lines)
                elements.extend(gap_elements)
            ocr_results = gap_results
        if compact and getattr(ctx, "flash_relation_hints", False) is True and elements:
            from artemis.utils.visualization import add_relation_hints

            minimal_list_str = add_relation_hints(minimal_list_str, elements, width, height)
        timings["list"] = round(time.monotonic() - phase_start, 3)
    except Exception as e:
        logger.error(f"Failed to fetch or fuse UI hierarchy: {e}")
        minimal_list_str = "Not available due to hierarchy error."
        hierarchy_ok = False

    screenshot_path = None
    phase_start = time.monotonic()
    # persist=False: a transient internal look (e.g. resolving target_text) that
    # nobody will show or replay; skip the data engine and the temp-file fallback.
    if persist and ctx.data_engine:
        try:
            image_name = ctx.data_engine.get_or_create_image(
                screenshot_bytes,
                ui_tree=xml_hierarchy,
                ocr_result=ocr_results,
            )
            screenshot_path = str(ctx.data_engine.get_image_path(image_name))
        except Exception as e:
            logger.error(f"Failed to save image in DataEngine: {e}")

    if persist and not screenshot_path:
        try:
            temp_dir = get_temp_dir("screenshots")
            temp_file = temp_dir / f"screenshot_{int(time.time())}.jpg"
            temp_file.write_bytes(screenshot_bytes)
            screenshot_path = str(temp_file)
        except Exception as e:
            logger.error(f"Failed to save fallback screenshot: {e}")

    from pathlib import Path

    timings["persist"] = round(time.monotonic() - phase_start, 3)
    return (
        ObserveResult(
            ok=True,
            message="Screen observed.",
            screenshot_path=screenshot_path,
            image_name=Path(screenshot_path).stem if screenshot_path else None,
            width=width,
            height=height,
            elements_text=minimal_list_str,
            elements=elements,
            hierarchy_ok=hierarchy_ok,
            scrollable=scrollable,
            timings=timings,
        ),
        screenshot_bytes,
    )
