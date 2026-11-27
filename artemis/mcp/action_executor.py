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

"""MCP-backed action executor for the FlashRunner.

Device actions use the in-process MCP session. Argument normalization,
element-index resolution, coordinate descriptions, smart swipes, post-action
observations, state updates, tracing, and helper tools are handled here on the
agent side.

Flash binds the same agent dialect as the Pro Operator: a ``click``,
``long_press`` or ``input_text`` target is either an element index into the
indexed ``--- Visible UI Elements ---`` list (resolved here to the element's
center, its observed text/bounds/id recorded) or a normalized ``[x, y]`` pair
carrying the model's own ``target_description``. The wire only ever sees
coordinates.

Device action status comes from ``ActionResult.ok``; helper tools report failures
through ``ToolFailure`` or an explicit status.
"""

import ast
import asyncio
import hashlib
import inspect
import json
import re
import time
from pathlib import Path
from typing import Any

from artemis.clients.screen_client_factory import DeviceOfflineError
from artemis.core.tool_failure import ToolFailure, is_tool_failure
from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.trace import TraceSpan
from artemis.mcp.action_manifest import OPTIONAL_ACTIONS, REQUIRED_ACTIONS
from artemis.mcp.action_specs import exception_prefix
from artemis.mcp.action_session import ActionSession, get_action_session
from artemis.mcp.action_types import ActionCode, ActionResult, is_device_offline_result
from artemis.mcp.actuators.adb import AdbActuator
from artemis.utils.image_hash import dhash_hex, hamming_distance_hex
from artemis.agents.validator.tool_declarations import (
    ToolExecutionResult,
    normalize_coordinate_target,
)
from artemis.tools.history import HISTORY_TOOL_NAMES, history_tool_by_name
from artemis.tools.tool_wrapper import split_multimodal_result
from artemis.utils.tap_snap import snap_to_named_element
from artemis.utils.coordinates import (
    compute_smart_swipe_coordinates,
    parse_swipe_parameters,
)
from artemis.utils.notes import (
    format_list_notes_failure,
    format_list_notes_success,
    format_read_note_failure,
    list_notes_info,
    read_note_content,
)
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["McpActionExecutor"]


#: Non-device tools executed agent-side (never behind the action server).
AGENT_TOOL_NAMES: frozenset[str] = (
    frozenset({"read_note", "list_notes", "ask_explorer", "video_analyzer"}) | HISTORY_TOOL_NAMES
)

#: Actions whose ``target`` is a single point: an element index or an [x, y] pair.
_POINT_TARGET_ACTIONS: frozenset[str] = frozenset({"click", "long_press", "input_text"})



def commitment_block_enabled() -> bool:
    """``ARTEMIS_BLOCK_COMMITMENT_TAPS``: refuse every tap on a commitment control (test runs)."""
    import os

    return os.environ.get("ARTEMIS_BLOCK_COMMITMENT_TAPS", "").strip().lower() in ("1", "true", "yes", "on")


def is_commitment_text(text: str) -> bool:
    """Commitment wording, also when OCR dropped the Vietnamese diacritics ("Dat don")."""
    import unicodedata

    from artemis.utils.screen_diff import COMMITMENT_PATTERN

    if COMMITMENT_PATTERN.search(text or ""):
        return True
    plain = unicodedata.normalize("NFKD", (text or "").replace("đ", "d").replace("Đ", "D"))
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return bool(COMMITMENT_PATTERN_ASCII.search(plain))


COMMITMENT_PATTERN_ASCII = re.compile(
    r"\b(place\s*order|dat\s+(?:don|hang)|pay\s+now|thanh\s+toan|confirm\s+purchase|xac\s+nhan\s+mua)\b",
    re.IGNORECASE,
)


def _timings(obs: Any) -> dict[str, float]:
    """Phase timings of an observation, empty when absent (debug turn log only)."""
    value = getattr(obs, "timings", None)
    return value if isinstance(value, dict) else {}

class _ArgError(ValueError):
    """Argument-translation failure whose message is already fully formatted."""


class _RepeatedTapError(_ArgError):
    """Repeated/ineffective tap rejection from the executor's local tap guards."""


class McpActionExecutor:
    """Routes agent tool calls through the unified action MCP session."""

    def __init__(
        self,
        ctx: ArtemisContext,
        controller: UnifiedMobileController | None = None,
        actuator: AdbActuator | None = None,
        agent_name: str = "validator",
    ):
        self.ctx = ctx
        self.actuator = actuator or getattr(ctx, "actuator", None) or AdbActuator(ctx, controller)
        self.controller = self.actuator.controller
        # Identifies the calling agent / profile to the Explorer tier resolver
        # (``explorer.flash_mode`` vs ``explorer.pro_mode``); the agent itself
        # never sees or chooses the tier.
        self.agent_name = agent_name
        self._session: ActionSession | None = None
        self._last_point_action: dict[str, Any] | None = None
        self._ineffective_taps: list[dict[str, Any]] = []
        self._last_elements_signature: str | None = None
        self._last_image_hash: str | None = None
        # dHash distance at or below which two screenshots count as the same
        # screen (calibrated in memory.transcript.similarity_max_distance:
        # same screen <=4, different screens >=7), so a clock tick or a
        # blinking cursor does not register as a screen change.
        self._same_screen_max_distance = 5
        try:
            from artemis.config import load_agent_config

            self._same_screen_max_distance = int(
                load_agent_config().memory.transcript.similarity_max_distance
            )
        except Exception as exc:
            logger.debug(f"Similarity distance config unavailable; using 5: {exc}")

    # Within ~2% of the screen (0-1000 scale), two taps aim at the same control.
    _SAME_POINT_SQ_DIST = 400

    @classmethod
    def _same_point(cls, a: Any, b: Any) -> bool:
        if not a or not b:
            return False
        dx = a[0] - b[0]
        dy = a[1] - b[1]
        return dx * dx + dy * dy <= cls._SAME_POINT_SQ_DIST

    def _note_interaction(self, raw_name: str, target_coordinates: Any) -> None:
        """Forgets ineffective taps that another interaction may have fixed.

        A tap that did nothing (a submit with a required option unset) becomes
        legitimate again once anything else on the screen was touched: an
        option picked, a field typed into, a toggle flipped, a scroll. Waits do
        not touch the screen, so they keep the memory. A custom-drawn option
        may change too little for the screen signature to notice, so the guard
        must not rely on seeing that change: any other interaction clears it.
        """
        if raw_name.startswith("wait"):
            return
        self._ineffective_taps = [
            e for e in self._ineffective_taps
            if self._same_point(e.get("target_coordinates"), target_coordinates)
        ]

    def _is_known_ineffective_tap(self, target_coordinates: Any) -> bool:
        """True when this point already had no effect on the current screen.

        "Current screen" needs the element signature AND (when both exist) the
        image hash to match: the 64-bit image hash alone cannot see a small
        change such as a selected option or a red outline turning green.
        """
        screen_sig = self._last_elements_signature
        if not screen_sig:
            return False
        screen_h = self._last_image_hash
        for entry in self._ineffective_taps:
            if entry.get("sig") != screen_sig:
                continue
            if screen_h and entry.get("hash"):
                dist = hamming_distance_hex(screen_h, entry["hash"])
                if dist is None or dist > self._same_screen_max_distance:
                    continue
            if self._same_point(entry.get("target_coordinates"), target_coordinates):
                return True
        return False

    def record_observation(self, elements: list[dict[str, Any]] | None, image_bytes: bytes | None) -> None:
        """Re-bases the screen-change baseline on an observation taken outside
        the executor (runner fallback capture, burst abort, scroll probe), so
        the next "screen unchanged" check compares against the screen the
        model actually saw last. Also clears the repeated-tap guard: the
        screen it was judged against is no longer the baseline."""
        self._last_elements_signature = self._elements_signature(elements)
        image_hash = dhash_hex(image_bytes) if image_bytes else None
        if image_hash:
            self._last_image_hash = image_hash
        self._last_point_action = None

    @staticmethod
    def _elements_signature(elements: list[dict[str, Any]] | None) -> str:
        if not elements:
            return ""
        parts = [
            f"{el.get('text')}:{el.get('center')}:{el.get('checked')}:{el.get('selected')}:{el.get('error')}"
            for el in elements
        ]
        return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()

    def _determine_settle_ms(
        self, raw_name: str, args: dict[str, Any], target_semantics: dict[str, Any]
    ) -> int:
        if raw_name == "manage_app":
            return 1500
        if raw_name == "press_key":
            key = str(args.get("key") or args.get("keycode") or "").upper()
            if key in ("ENTER", "66", "SEARCH", "84"):
                return 1000
        return 400

    @property
    def action_tool_names(self) -> frozenset[str]:
        """Device actions plus backend extension names -- the dynamic dispatch set."""
        return (REQUIRED_ACTIONS | OPTIONAL_ACTIONS) | {e.name for e in self.actuator.extensions()}

    async def _session_or_start(self) -> ActionSession:
        if self._session is None or not self._session.started:
            self._session = await get_action_session(self.ctx, actuator=self.actuator)
        return self._session

    # --- Public entry ----------------------------------------------------------------

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        tool_call_id: str,
        state: Any,
        *,
        index_elements: list[dict[str, Any]] | None = None,
        skip_observe: bool = False,
        verify_live_target: bool = False,
    ) -> ToolExecutionResult:
        """Executes the named tool and wraps the outcome for the calling agent.

        ``index_elements`` is the element list an index target (``click(3)``)
        resolves against; ``None`` means ``state.indexed_elements`` at call
        time. A multi-action turn passes the list from the observation the
        model decided on, so its later indices are not resolved against the
        list refreshed by its earlier actions (``state.indexed_elements`` is
        still refreshed after every action: it is what the next observation
        shows).
        """
        raw_name = name.split(":")[-1] if ":" in name else name

        if raw_name in AGENT_TOOL_NAMES:
            return await self._execute_agent_tool(raw_name, name, args, tool_call_id, state)

        if raw_name in self.action_tool_names:
            return await self._execute_device_action(
                raw_name,
                name,
                args,
                tool_call_id,
                state,
                index_elements=index_elements,
                skip_observe=skip_observe,
                verify_live_target=verify_live_target,
            )

        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            tool_name=name,
            status="error",
            text_summary=f"Error: Tool '{name}' not supported.",
        )

    # --- Device actions --------------------------------------------------------------

    async def _execute_device_action(
        self,
        raw_name: str,
        name: str,
        args: dict[str, Any],
        tool_call_id: str,
        state: Any,
        *,
        index_elements: list[dict[str, Any]] | None = None,
        skip_observe: bool = False,
        verify_live_target: bool = False,
    ) -> ToolExecutionResult:
        span = TraceSpan(name=raw_name, trace_type="action", ctx=self.ctx)
        span.payload = {"args": args}

        message = ""
        res: ActionResult | None = None
        img_bytes: bytes | None = None
        shot_path: str | None = None
        xml_list: str | None = None
        target_semantics: dict[str, Any] = {}
        target_coordinates: list[int] | None = None
        action_s: float | None = None
        observe_s: float | None = None
        observe_detail: list[dict[str, Any]] = []
        device_offline = False
        ineffective_tap = False
        with span:
            try:
                session = await self._session_or_start()
                if raw_name in _POINT_TARGET_ACTIONS and args.get("target_text"):
                    target_clean = str(args.get("target_text")).strip()
                    if not target_clean:
                        raise _ArgError(f"Error during {raw_name}: target_text cannot be empty.")

                    from artemis.utils.screen_diff import COMMITMENT_PATTERN
                    if COMMITMENT_PATTERN.search(target_clean):
                        raise _ArgError(
                            f"Commitment Guard: Action '{raw_name}' targeting commitment text '{target_clean}' "
                            f"cannot be executed via target_text. Please verify the screen and use element index or explicit confirmation."
                        )

                    # target_text always resolves against the live screen, never
                    # against the turn's snapshot: an earlier action in the burst
                    # may have changed the screen, and the same text at its old
                    # position would be tapped on the new screen. Poll up to 3s
                    # for the text; if it never shows up, resolve against the last
                    # live observation so the Anchor Guard reports "not found".
                    last_live_elements: list[dict[str, Any]] | None = None
                    if session is not None and hasattr(session, "observe"):
                        from artemis.utils.screen_diff import find_matching_text_elements

                        deadline = time.monotonic() + 3.0
                        while True:
                            try:
                                obs = await session.observe(settle_ms=0, persist=False)
                                if (
                                    obs
                                    and obs.ok
                                    and obs.hierarchy_ok
                                    and isinstance(obs.elements, list)
                                ):
                                    last_live_elements = obs.elements
                                    width, height = self._screen_size()
                                    if obs.elements and find_matching_text_elements(
                                        obs.elements, target_clean, width=width, height=height
                                    ):
                                        break
                            except Exception as e:
                                logger.debug(f"Error observing fresh screen for target_text: {e}")
                            if time.monotonic() >= deadline:
                                break
                            await asyncio.sleep(0.3)

                    if last_live_elements is None:
                        raise _ArgError(
                            f"Anchor Guard: could not observe the live screen to resolve "
                            f"target_text '{target_clean}'. Aborting chain to request a fresh AI observation."
                        )
                    index_elements = last_live_elements
                    if state is not None:
                        state.indexed_elements = last_live_elements
                        state.indexed_points = [
                            el["center"] for el in last_live_elements if el.get("center")
                        ]

                wire_name, wire_args, finalize, target_semantics = self._translate(
                    raw_name, args, state, index_elements=index_elements
                )
                if raw_name in _POINT_TARGET_ACTIONS:
                    # The normalized point the action was sent to: the recorded
                    # coordinates of an index target (the model named an index,
                    # not a point).
                    target_coordinates = wire_args.get("target")
                    if target_coordinates:
                        # 1. Immediate consecutive repeated tap check
                        if self._last_point_action:
                            last_coords = self._last_point_action.get("target_coordinates")
                            last_unchanged = self._last_point_action.get("screen_unchanged", False)
                            if last_coords and last_unchanged:
                                dx = target_coordinates[0] - last_coords[0]
                                dy = target_coordinates[1] - last_coords[1]
                                if (dx * dx + dy * dy) <= 400:  # <= 20 units (~2% threshold)
                                    raise _RepeatedTapError(
                                        f"Repeated tap rejected: Point {target_coordinates} was already tapped in the "
                                        f"previous step and did not change the screen. If you are confirming a form, "
                                        f"please check if there is an unselected option, required field, or validation notice on screen."
                                    )

                        # 2. Pure-repetition check: the same point was already tapped on
                        # this exact screen with no effect, and nothing was interacted
                        # with since (waits do not count). See _note_interaction.
                        if self._is_known_ineffective_tap(target_coordinates):
                            raise _RepeatedTapError(
                                f"Repeated tap rejected: Point {target_coordinates} was already tapped on this "
                                f"screen and did not change anything, and nothing else was changed since. "
                                f"Inspect the screen for an unselected required option, a disabled control, "
                                f"a validation notice or content below the fold before trying again."
                            )

                        # 3. Morphed-commitment guard: later in a burst the screen may
                        # have changed under a point chosen on the turn's screenshot. A
                        # control that turned into a commitment button ("Review address"
                        # becoming "Place Order" at the same spot) must not be tapped
                        # blind. target_text taps carry their own Commitment Guard.
                        if verify_live_target and not args.get("target_text"):
                            await self._guard_morphed_commitment(
                                session, target_coordinates, state, index_elements
                            )
                if commitment_block_enabled():
                    block_points: list[Any] = []
                    if raw_name in _POINT_TARGET_ACTIONS and wire_args.get("target"):
                        block_points = [wire_args.get("target")]
                    elif raw_name == "click_sequence":
                        block_points = list(wire_args.get("sequence") or [])
                    if block_points:
                        await self._guard_commitment_block(session, block_points, state, index_elements)
                extension = raw_name not in (REQUIRED_ACTIONS | OPTIONAL_ACTIONS)

                action_started = time.monotonic()
                if extension:
                    raw_result = await session.call_raw(wire_name, wire_args)
                    message = next(
                        (b.text for b in raw_result.content if getattr(b, "type", "") == "text"),
                        f"Extension tool '{raw_name}' completed.",
                    )
                    res = ActionResult.success(raw_name, message)
                else:
                    res = await session.call(wire_name, wire_args)
                    if res.ok and target_semantics.get("snapped_from"):
                        res_note = (
                            f" Snapped from your estimate {target_semantics['snapped_from']}"
                            f" to the center of '{target_semantics.get('snapped_to_text')}'."
                        )
                        res = res.model_copy(update={"message": res.message + res_note})
                    message = finalize(res) if finalize else res.message
                action_s = time.monotonic() - action_started
                if res.ok:
                    self._note_interaction(raw_name, target_coordinates)
                if is_device_offline_result(res):
                    device_offline = True

                if (res.ok or self._observe_despite_failure(raw_name, res)) and not skip_observe:
                    observe_started = time.monotonic()
                    settle_ms = self._determine_settle_ms(raw_name, args, target_semantics)
                    settled = await self._wait_screen_settled(session, raw_name, args, settle_ms)
                    obs = await session.observe(settle_ms=0 if settled else settle_ms)
                    observe_detail.append({"why": "post", **_timings(obs), **(settled or {})})
                    if is_device_offline_result(obs):
                        device_offline = True
                        if res.ok:
                            res = ActionResult.failure(
                                raw_name,
                                obs.message,
                                code=ActionCode.DEVICE_ERROR,
                                detail=obs.message,
                            )
                            message = obs.message
                    if obs.ok:
                        # Auto-detect skeleton / loading (<=1 text element in content area below top bar)
                        if obs.hierarchy_ok and len(obs.elements) > 3 and (raw_name in ("manage_app", "press_key") or settle_ms >= 1000):
                            screen_height = obs.height or 2400
                            content_y_threshold = screen_height * 0.15
                            content_text_elements = [
                                el for el in obs.elements
                                if el.get("text") and el.get("center") and el["center"][1] > content_y_threshold
                            ]
                            if len(content_text_elements) <= 1:
                                logger.info(
                                    f"Screen appears to be skeleton/loading (content area below top bar has {len(content_text_elements)} text element); waiting additional 1.0s..."
                                )
                                obs = await session.observe(settle_ms=1000)
                                observe_detail.append({"why": "skeleton", **_timings(obs)})

                        shot_path = obs.screenshot_path
                        xml_list = obs.elements_text
                        if shot_path:
                            try:
                                img_bytes = Path(shot_path).read_bytes()
                            except Exception as read_err:
                                logger.warning(f"Failed to read observed screenshot: {read_err}")

                        # Screen unchanged calculation combining XML elements and image dHash (app-agnostic)
                        def _calc_unchanged(obs_res: Any, shot_data: bytes | None) -> tuple[bool, str, str | None]:
                            sig = self._elements_signature(obs_res.elements)
                            xml_same = bool(
                                self._last_elements_signature is not None and sig == self._last_elements_signature
                            )
                            img_same = True
                            img_h = dhash_hex(shot_data) if shot_data else None
                            if self._last_image_hash and img_h:
                                dist = hamming_distance_hex(img_h, self._last_image_hash)
                                if dist is not None and dist > self._same_screen_max_distance:
                                    img_same = False
                            return (xml_same and img_same), sig, img_h

                        screen_unchanged, curr_sig, curr_hash = _calc_unchanged(obs, img_bytes)

                        # Retry observation if screen is unchanged immediately after a tap (permits page transition / bottom sheet to finish animation)
                        already_waited = bool(
                            settled and settled.get("expects_change") and not settled.get("changed")
                        )
                        if raw_name in _POINT_TARGET_ACTIONS and screen_unchanged and not already_waited:
                            logger.info(
                                "Screen unchanged immediately after tap; waiting 800ms for potential page transition..."
                            )
                            retry_obs = await session.observe(settle_ms=800)
                            observe_detail.append({"why": "unchanged_retry", **_timings(retry_obs)})
                            if retry_obs.ok:
                                retry_shot = retry_obs.screenshot_path
                                retry_bytes = None
                                if retry_shot:
                                    try:
                                        retry_bytes = Path(retry_shot).read_bytes()
                                    except Exception:
                                        pass
                                retry_unchanged, retry_sig, retry_h = _calc_unchanged(retry_obs, retry_bytes)
                                if not retry_unchanged:
                                    logger.info("Screen transition detected after 800ms settle delay.")
                                    obs = retry_obs
                                    shot_path = retry_shot
                                    img_bytes = retry_bytes
                                    xml_list = obs.elements_text
                                    screen_unchanged = False
                                    curr_sig = retry_sig
                                    curr_hash = retry_h

                        if screen_unchanged:
                            element_errors = []
                            if obs.hierarchy_ok and obs.elements:
                                for el in obs.elements:
                                    err = el.get("error")
                                    if err:
                                        element_errors.append(str(err))
                            if element_errors:
                                message += f" (no visible change; element error: '{element_errors[0]}')"
                            else:
                                message += " (no visible change)"

                        self._last_elements_signature = curr_sig
                        if curr_hash:
                            self._last_image_hash = curr_hash

                        if obs.hierarchy_ok:
                            state.indexed_points = [el["center"] for el in obs.elements]
                            state.indexed_elements = obs.elements
                            state.screen_scrollable = obs.scrollable

                        # Reset or update _last_point_action and ineffective taps memory
                        if raw_name in _POINT_TARGET_ACTIONS and target_coordinates:
                            if screen_unchanged:
                                self._last_point_action = {
                                    "raw_name": raw_name,
                                    "target_coordinates": target_coordinates,
                                    "screen_unchanged": True,
                                }
                                self._ineffective_taps.append({
                                    "target_coordinates": target_coordinates,
                                    "sig": curr_sig,
                                    "hash": curr_hash,
                                })
                                del self._ineffective_taps[:-10]
                            else:
                                self._last_point_action = None
                        else:
                            # Any non-point action (swipe, wait, press_key, etc.) resets point action guard
                            self._last_point_action = None
                    observe_s = time.monotonic() - observe_started
                elif res.ok and skip_observe:
                    # Intermediate burst action: short delay between consecutive physical inputs
                    await asyncio.sleep(0.1)
                    if target_coordinates:
                        self._last_point_action = {
                            "raw_name": raw_name,
                            "target_coordinates": target_coordinates,
                            "screen_unchanged": False,
                        }
                    else:
                        self._last_point_action = None
            except _RepeatedTapError as e:
                ineffective_tap = True
                res = ActionResult.failure(raw_name, str(e), code=ActionCode.INVALID_ARGS)
                message = str(e)
            except _ArgError as e:
                res = ActionResult.failure(raw_name, str(e), code=ActionCode.INVALID_ARGS)
                message = str(e)
            except Exception as e:
                if isinstance(e, DeviceOfflineError):
                    device_offline = True
                message = f"{exception_prefix(raw_name)}: {e}"
                res = ActionResult.failure(raw_name, message, detail=repr(e))

            if shot_path:
                state.latest_screenshot = shot_path

            status = "success" if res.ok else "error"
            post_image_name = None
            if shot_path:
                post_image_name = Path(shot_path).stem
            elif img_bytes:
                post_image_name = hashlib.sha256(img_bytes).hexdigest()
            span.result = {
                "outcome": message,
                "post_image_name": post_image_name,
                "has_xml": bool(xml_list),
                "status": status,
            }
            if status == "error":
                span.status = "failed"
                span.error = message

        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            tool_name=name,
            status=status,
            text_summary=message,
            screenshot_bytes=img_bytes,
            screenshot_path=shot_path,
            ui_elements_text=xml_list,
            metadata={
                "code": res.code.value,
                "normalized_coordinates": res.normalized_coordinates,
                "target_semantics": target_semantics,
                "target_coordinates": target_coordinates,
                "timed_out": getattr(res, "timed_out", False),
                "no_effect": getattr(res, "no_effect", False),
                "ineffective_tap": ineffective_tap,
                "device_offline": device_offline or is_device_offline_result(res),
                # Wall time of the device action itself and of the post-action
                # observation (settle + screenshot + UI hierarchy), in seconds.
                "action_s": round(action_s, 3) if action_s is not None else None,
                "observe_s": round(observe_s, 3) if observe_s is not None else None,
                # Per-capture phase timings (debug turn log only).
                "observe_detail": observe_detail or None,
            },
        )

    # --- Wait for the UI to settle (Flash) ---------------------------------------------
    # Measured on an emulator (dHash, 64 bits): consecutive frames of a still
    # screen differ by 0-2 bits (blinking cursor, clock), frames of a transition
    # by 12-35; a tap's effect starts ~0.3-0.5 s after it is injected.
    _SETTLE_STILL_BITS = 2
    _SETTLE_CHANGE_WINDOW_S = 1.0
    _SETTLE_CAP_S = 3.0
    # Actions that usually move to another screen: first wait for the change to
    # start, else a still frame of the OLD screen would pass as "settled".
    _SETTLE_EXPECTS_CHANGE = frozenset({"click", "long_press", "click_sequence", "press_key", "manage_app"})

    async def _wait_screen_settled(
        self, session: Any, raw_name: str, args: dict[str, Any], settle_ms: int
    ) -> dict[str, Any] | None:
        """Waits until the screen stopped moving instead of a fixed delay.

        Phase 1 (actions that usually change the screen): poll quick
        screenshots until the frame differs from the pre-action baseline, up to
        ``_SETTLE_CHANGE_WINDOW_S``. Phase 2: until two consecutive frames are
        within ``_SETTLE_STILL_BITS``, up to ``_SETTLE_CAP_S`` in total. Returns
        the measurements, or None when quick screenshots are unavailable (the
        caller then falls back to the fixed settle delay). Flash only.
        """
        if self.agent_name != "flash" or not hasattr(session, "screenshot_b64"):
            return None
        import base64 as _b64

        async def _frame_hash() -> str | None:
            b64 = await session.screenshot_b64(timeout=2.0)
            if not isinstance(b64, str):
                raise TypeError("screenshot_b64 returned no string")
            return dhash_hex(_b64.b64decode(b64))

        started = time.monotonic()
        try:
            if raw_name == "manage_app":
                # A splash screen can sit still before the app's first screen.
                await asyncio.sleep(settle_ms / 1000.0)
            key = str(args.get("key") or "").upper()
            expects_change = raw_name in self._SETTLE_EXPECTS_CHANGE and not (
                raw_name == "press_key" and key not in ("ENTER", "66", "SEARCH", "84", "BACK", "4", "HOME", "3")
            )
            baseline = self._last_image_hash
            changed = False
            prev = await _frame_hash()
            frames = 1
            if expects_change and baseline and prev:
                change_deadline = time.monotonic() + self._SETTLE_CHANGE_WINDOW_S
                while (hamming_distance_hex(prev, baseline) or 0) <= self._SETTLE_STILL_BITS:
                    if time.monotonic() >= change_deadline:
                        break
                    prev = await _frame_hash()
                    frames += 1
                changed = (hamming_distance_hex(prev, baseline) or 0) > self._SETTLE_STILL_BITS
            stable = False
            cap = started + self._SETTLE_CAP_S + (settle_ms / 1000.0 if raw_name == "manage_app" else 0.0)
            while time.monotonic() < cap:
                cur = await _frame_hash()
                frames += 1
                if baseline and not changed:
                    changed = (hamming_distance_hex(cur, baseline) or 0) > self._SETTLE_STILL_BITS
                dist = hamming_distance_hex(prev, cur)
                prev = cur
                if dist is not None and dist <= self._SETTLE_STILL_BITS:
                    stable = True
                    break
        except Exception as exc:
            logger.debug(f"Settle wait unavailable, using fixed delay: {exc}")
            return None
        waited = round(time.monotonic() - started, 3)
        logger.info(
            f"UI settle after {raw_name}: {'stable' if stable else 'still moving (cap)'}"
            f" in {waited}s, {frames} frames, changed={changed}"
        )
        return {"settle_s": waited, "frames": frames, "stable": stable, "changed": changed,
                "expects_change": expects_change}

    @staticmethod
    def _observe_despite_failure(raw_name: str, res: ActionResult) -> bool:
        """Capture the screen after launch failures and wait_for_text timeouts.

        Argument and package lookup errors do not require an observation.
        """
        if raw_name == "manage_app":
            return res.code not in (ActionCode.PACKAGE_NOT_FOUND, ActionCode.INVALID_ARGS)
        if raw_name in ("wait_for_text", "wait_for_screen_change", "wait_until_gone"):
            return res.code is ActionCode.TIMEOUT or res.code is ActionCode.DEVICE_ERROR
        return False

    # --- Argument translation --------------------------------------------------------

    def _translate(
        self,
        raw_name: str,
        args: dict[str, Any],
        state: Any,
        *,
        index_elements: list[dict[str, Any]] | None = None,
    ) -> tuple[str, dict[str, Any], Any, dict[str, Any]]:
        """Maps agent-facing args to wire args; returns (name, args, finalize, recorded).

        ``finalize(res)`` adds client-side context to swipe result messages.
        ``recorded`` holds the target semantics in the Pro Operator's record
        format: the observed element fields of an index target, or the validated
        ``target_description`` of a coordinate target. Focused input and
        directional swipes have no target, so they record nothing.
        ``index_elements`` overrides the list index targets resolve against
        (see :meth:`execute`).
        """
        if raw_name == "click":
            target, recorded = self._resolve_target(
                args.get("target") or args.get("coordinates") or args.get("target_text"),
                "click",
                args,
                state,
                index_elements=index_elements,
            )
            return (
                "click",
                {
                    "target": target,
                    "times": args.get("times", 1),
                    "delay_ms": args.get("delay_ms", 100),
                },
                None,
                recorded,
            )

        if raw_name == "long_press":
            target, recorded = self._resolve_target(
                args.get("target") or args.get("coordinates") or args.get("target_text"),
                "long press",
                args,
                state,
                index_elements=index_elements,
            )
            return (
                "long_press",
                {
                    "target": target,
                    "duration_ms": args.get("duration_ms", args.get("duration", 1000)),
                },
                None,
                recorded,
            )

        if raw_name == "input_text":
            raw_target = args.get("target") or args.get("coordinates") or args.get("target_text")
            target = None
            recorded = {}
            if raw_target:
                target, recorded = self._resolve_target(
                    raw_target, "input text", args, state, index_elements=index_elements
                )
            return (
                "input_text",
                {
                    "text": args.get("text", ""),
                    "target": target,
                    "clear_exist": args.get("clear_exist", True),
                },
                None,
                recorded,
            )

        if raw_name == "click_sequence":
            sequence = self._resolve_sequence(args.get("sequence") or [])
            recorded = self._require_descriptions(args, len(sequence))
            return (
                "click_sequence",
                {
                    "sequence": sequence,
                    "delay_ms": args.get("delay_ms", 50),
                },
                None,
                recorded,
            )

        if raw_name == "swipe":
            return self._translate_swipe(args, state, index_elements=index_elements)

        if raw_name == "press_key":
            return "press_key", {"key": args.get("key", "BACK")}, None, {}

        if raw_name == "manage_app":
            return (
                "manage_app",
                {
                    "action": args.get("action", "launch"),
                    "app_name": args.get("app_name", ""),
                },
                None,
                {},
            )

        if raw_name == "wait_for_delay":
            return "wait_for_delay", {"time_in_ms": args.get("time_in_ms", 1000)}, None, {}

        if raw_name == "wait_for_screen_change":
            timeout_s = args.get("timeout_s")
            timeout_ms = int(timeout_s * 1000) if timeout_s is not None else args.get("timeout_ms", 25000)
            return (
                "wait_for_screen_change",
                {
                    "timeout_ms": timeout_ms,
                    "region": args.get("region"),
                    "after": args.get("after", "my_action"),
                },
                None,
                {},
            )

        if raw_name == "wait_for_text":
            timeout_s = args.get("timeout_s")
            timeout_ms = int(timeout_s * 1000) if timeout_s is not None else args.get("timeout_ms", 8000)
            return (
                "wait_for_text",
                {
                    "text": args.get("text", ""),
                    "wait_state": args.get("wait_state") or "appear",
                    "timeout_ms": timeout_ms,
                },
                None,
                {},
            )

        if raw_name == "wait_until_gone":
            timeout_s = args.get("timeout_s")
            timeout_ms = int(timeout_s * 1000) if timeout_s is not None else args.get("timeout_ms", 15000)
            return (
                "wait_for_text",
                {
                    "text": args.get("text", ""),
                    "wait_state": "disappear",
                    "timeout_ms": timeout_ms,
                },
                None,
                {},
            )

        # Backend extension: pass the arguments straight through.
        return raw_name, dict(args), None, {}

    def _screen_size(self) -> tuple[int, int]:
        """The device resolution the indexed element list was built against."""
        device = getattr(self.ctx, "device", None)
        width = getattr(device, "device_width", None) if device else None
        height = getattr(device, "device_height", None) if device else None
        return (
            width if isinstance(width, int) and width > 0 else 1080,
            height if isinstance(height, int) and height > 0 else 2400,
        )

    def _resolve_target_text(
        self,
        target_text: str,
        label: str,
        state: Any,
        *,
        index_elements: list[dict[str, Any]] | None = None,
    ) -> tuple[list[int], dict[str, Any]]:
        target_clean = target_text.strip()
        if not target_clean:
            raise _ArgError(f"Error during {label}: target_text cannot be empty.")

        # 1. Commitment Guard: Never allow commitment button targeting via target_text on unseen screens
        from artemis.utils.screen_diff import COMMITMENT_PATTERN, find_matching_text_elements

        if COMMITMENT_PATTERN.search(target_clean):
            raise _ArgError(
                f"Commitment Guard: Action '{label}' targeting commitment text '{target_clean}' "
                f"cannot be executed via target_text. Please verify the screen and use element index or explicit confirmation."
            )

        elements = self._index_elements(state, index_elements) if state is not None else index_elements
        width, height = self._screen_size()

        matches = find_matching_text_elements(elements, target_clean, width=width, height=height)

        if not matches:
            raise _ArgError(
                f"Anchor Guard: target_text '{target_clean}' for {label} was not found on screen. "
                f"Aborting chain to request fresh AI observation."
            )

        # 2. Ambiguity Guard: Multiple candidates with top match score at different screen locations
        top_score = matches[0].get("_match_score", 1)
        top_matches = [m for m in matches if m.get("_match_score") == top_score]
        if len(top_matches) > 1:
            centers = []
            for m in top_matches:
                c = m.get("center")
                if not c:
                    b = m.get("_parsed_bounds") or m.get("bounds")
                    if isinstance(b, (list, tuple)) and len(b) == 4:
                        c = [(b[0] + b[2]) // 2, (b[1] + b[3]) // 2]
                if c:
                    centers.append(c)
            if len(centers) > 1:
                c0 = centers[0]
                distinct = any(abs(c[0] - c0[0]) > 40 or abs(c[1] - c0[1]) > 40 for c in centers[1:])
                if distinct:
                    raise _ArgError(
                        f"Anchor Guard: target_text '{target_clean}' for {label} is ambiguous "
                        f"({len(top_matches)} matches found at different positions). "
                        f"Aborting chain to request AI decision."
                    )

        best_match = matches[0]

        # 3. Occlusion Guard
        if best_match.get("occluded") or best_match.get("is_occluded"):
            raise _ArgError(
                f"Anchor Guard: target_text '{target_clean}' for {label} is occluded or obscured. "
                f"Aborting chain."
            )

        # 4. Resolve normalized [x, y] coordinates
        center = best_match.get("center")
        if center and len(center) == 2:
            nx = int(max(0, min(1000, round(center[0] * 1000 / max(1, width)))))
            ny = int(max(0, min(1000, round(center[1] * 1000 / max(1, height)))))
            point = [nx, ny]
        else:
            bounds = best_match.get("_parsed_bounds") or best_match.get("bounds")
            if bounds and len(bounds) == 4:
                l, t, r, b = bounds
                cx = (l + r) // 2
                cy = (t + b) // 2
                nx = int(max(0, min(1000, round(cx * 1000 / max(1, width)))))
                ny = int(max(0, min(1000, round(cy * 1000 / max(1, height)))))
                point = [nx, ny]
            else:
                raise _ArgError(f"Anchor Guard: target_text '{target_clean}' has no valid coordinates.")

        recorded = {
            "target_text": target_clean,
            "target_bounds": best_match.get("bounds"),
            "target_class": best_match.get("class"),
            "target_resource_id": best_match.get("resource_id") or best_match.get("resource-id"),
            "snapped_to_text": best_match.get("text") or target_clean,
        }
        return point, recorded

    def _resolve_target(
        self,
        raw: Any,
        label: str,
        args: dict[str, Any],
        state: Any,
        *,
        index_elements: list[dict[str, Any]] | None = None,
    ) -> tuple[list[int], dict[str, Any]]:
        """Resolves a point target to ``([x, y] normalized, recorded semantics)``.

        An element index carries observed semantics (text, bounds, id, class
        straight from the indexed list); a coordinate pair carries only the
        model's own ``target_description``. The two never mix (Pro parity).
        Supports dynamic element targeting via ``target_text`` with Anchor Guard.
        """
        target_text_val = args.get("target_text")
        if target_text_val:
            return self._resolve_target_text(str(target_text_val).strip(), label, state, index_elements=index_elements)

        target = normalize_coordinate_target(raw)
        if isinstance(target, bool):
            target = None
        if isinstance(target, (int, float)):
            return self._resolve_index(int(target), label, state, index_elements=index_elements)
        if isinstance(target, (list, tuple)) and len(target) == 2:
            recorded = self._require_description(args, label)
            point = [int(target[0]), int(target[1])]
            # A model-estimated point is moved to the center of the listed
            # element its description names, when it lands on or near it.
            width, height = self._screen_size()
            snap = snap_to_named_element(
                point,
                recorded.get("target_description"),
                self._index_elements(state, index_elements) if state is not None else index_elements,
                width,
                height,
            )
            if snap is not None and snap.point != point:
                logger.info(
                    f"Snapped {label} {point} -> {snap.point} (center of '{snap.element_text}')"
                )
                recorded = {
                    **recorded,
                    "snapped_from": point,
                    "snapped_to_text": snap.element_text,
                    "target_bounds": snap.element_bounds,
                }
                point = snap.point
            return point, recorded
        raise _ArgError(
            f"Error during {label}: Invalid target format: {raw!r}. Use an element"
            " index from the Visible UI Elements list (e.g. 3) or normalized [x, y]"
            " coordinates with a target_description."
        )

    def _resolve_index(
        self,
        index: int,
        label: str,
        state: Any,
        *,
        index_elements: list[dict[str, Any]] | None = None,
    ) -> tuple[list[int], dict[str, Any]]:
        """Maps an element index onto the element's center (normalized 0-1000).

        The recorded fields are what the Operator records for an index target,
        with bounds normalized like every other Flash coordinate. The index is
        resolved against ``index_elements`` when given, else against the
        state's current list.
        """
        elements = self._index_elements(state, index_elements)
        if not 1 <= index <= len(elements):
            hint = (
                " The list is empty on this screen."
                if not elements
                else f" Active index range is 1 to {len(elements)}."
            )
            raise _ArgError(
                f"Error during {label}: Invalid target index {index}.{hint} Use an index"
                " shown in the Visible UI Elements list, or normalized [x, y]"
                " coordinates with a target_description (ask_explorer can locate an"
                " element that is visible but not listed)."
            )
        element = elements[index - 1]
        width, height = self._screen_size()

        def _norm_x(v: Any) -> int:
            return int(max(0, min(1000, round(float(v) * 1000 / max(1, width)))))

        def _norm_y(v: Any) -> int:
            return int(max(0, min(1000, round(float(v) * 1000 / max(1, height)))))

        center = element.get("center")
        if not (isinstance(center, (list, tuple)) and len(center) == 2):
            raise _ArgError(f"Error during {label}: element [{index}] has no usable center.")
        bounds = element.get("bounds")
        norm_bounds = None
        if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
            norm_bounds = [
                _norm_x(bounds[0]),
                _norm_y(bounds[1]),
                _norm_x(bounds[2]),
                _norm_y(bounds[3]),
            ]
        recorded = {
            "target_text": element.get("text"),
            "target_bounds": norm_bounds,
            "target_resource_id": element.get("resource_id"),
            "target_class": element.get("class"),
        }
        return [_norm_x(center[0]), _norm_y(center[1])], recorded

    @staticmethod
    def _require_description(args: dict[str, Any], label: str) -> dict[str, Any]:
        """Validate and trim the coordinate target description for the action record."""
        description = args.get("target_description")
        if not (isinstance(description, str) and description.strip()):
            raise _ArgError(
                f"Error during {label}: 'target_description' is required (what the"
                " target is, in a few words)."
            )
        return {"target_description": description.strip()}

    @staticmethod
    def _require_descriptions(args: dict[str, Any], count: int) -> dict[str, Any]:
        """``click_sequence``: one description per sequence entry, in order.

        Returns the cleaned statements as the recorded ``target_descriptions`` field.
        """
        descriptions = args.get("target_descriptions")
        if isinstance(descriptions, str):
            descriptions = [descriptions]
        if (
            not isinstance(descriptions, (list, tuple))
            or len(descriptions) != count
            or not all(isinstance(d, str) and d.strip() for d in descriptions)
        ):
            raise _ArgError(
                "Error during click sequence: 'target_descriptions' is required, one"
                f" non-empty entry per sequence entry ({count} expected)."
            )
        return {"target_descriptions": [d.strip() for d in descriptions]}

    @staticmethod
    def _resolve_sequence(sequence: Any) -> list[list[int]]:
        """Normalize click_sequence entries to coordinate pairs; reject element indices."""
        if isinstance(sequence, str):
            sequence_str = sequence.strip()
            try:
                sequence = json.loads(sequence_str)
            except ValueError:
                try:
                    sequence = ast.literal_eval(sequence_str)
                except (ValueError, SyntaxError, TypeError, RecursionError):
                    pass

        if not isinstance(sequence, (list, tuple)):
            raise _ArgError(f"Error during click sequence: Invalid sequence format: {sequence}")

        resolved: list[list[int]] = []
        for position, raw_target in enumerate(sequence, start=1):
            target = normalize_coordinate_target(raw_target)
            if isinstance(target, (int, float)) and not isinstance(target, bool):
                raise _ArgError(
                    f"Error during click sequence: entry {position} ({raw_target!r}) is an"
                    " element index; click_sequence takes normalized [x, y] coordinate"
                    " pairs only. Use the element's coordinates from the element list,"
                    " or ask_explorer to locate it."
                )
            if isinstance(target, (list, tuple)) and len(target) == 2:
                nx, ny = int(target[0]), int(target[1])
            else:
                raise _ArgError(f"Error during click sequence: Invalid target format: {raw_target}")
            resolved.append([nx, ny])
        return resolved

    def _label_at(self, elements: Any, point: Any) -> str:
        """Text of the smallest labelled element containing ``point`` (0-1000)."""
        from artemis.utils.screen_diff import flatten_hierarchy, robust_parse_bounds

        if not elements or not point:
            return ""
        width, height = self._screen_size()
        px = point[0] * width / 1000.0
        py = point[1] * height / 1000.0
        best: tuple[float, str] | None = None
        for el in flatten_hierarchy(elements):
            label = str(
                el.get("text") or el.get("content-desc") or el.get("content_description") or ""
            ).strip()
            if not label:
                continue
            bounds = robust_parse_bounds(el.get("bounds"))
            if not bounds:
                continue
            l, t, r, b = bounds
            if l <= px <= r and t <= py <= b:
                area = float(max(1, (r - l) * (b - t)))
                if best is None or area < best[0]:
                    best = (area, label)
        return best[1] if best else ""

    async def _guard_commitment_block(
        self,
        session: Any,
        points: list[Any],
        state: Any,
        index_elements: list[dict[str, Any]] | None,
    ) -> None:
        """Hard block (``ARTEMIS_BLOCK_COMMITMENT_TAPS``): no tap may land on a commitment control.

        For test runs on real accounts. Each point is checked against the element
        list the model decided on, the live hierarchy, and local OCR of the live
        screenshot around the point (a pinned button often has no node). Fails
        closed: when the live screen cannot be read, the tap is refused.
        """
        width, height = self._screen_size()
        seen = self._index_elements(state, index_elements)
        try:
            live = await session.ui_hierarchy(timeout=2.0)
            shot_b64 = await session.screenshot_b64(timeout=5.0)
        except Exception as err:  # pylint: disable=broad-exception-caught
            raise _ArgError(
                f"Commitment block: the live screen could not be read ({err}); tap refused."
            ) from err
        import base64

        from artemis.utils.local_ocr import local_ocr_settings, read_unread, recognize_screenshot
        from artemis.utils.visualization import _merge_ocr_words, _ocr_box

        shot = base64.b64decode(shot_b64) if shot_b64 else b""
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            for source, label in (("listed", self._label_at(seen, point)), ("live", self._label_at(live, point))):
                if label and is_commitment_text(label):
                    raise _ArgError(
                        f"Commitment block: the control at {list(point)} is '{label}' ({source} element)."
                        " Commitment actions (placing an order, paying) are disabled for this run;"
                        " stop here and report the task status instead."
                    )
            if not shot:
                raise _ArgError("Commitment block: no screenshot to check the tap point; tap refused.")
            px, py = point[0] * width / 1000.0, point[1] * height / 1000.0
            band = (max(0, int(py - height * 0.06)), min(height, int(py + height * 0.06)))
            mx, my = width * 0.03, height * 0.015
            try:
                cfg = local_ocr_settings(self.ctx)
                lines = await asyncio.to_thread(recognize_screenshot, shot, live, cfg, band)
                # VietOCR mode only located the lines: read the ones at the tap point.
                spot = [(int(px - mx), int(py - my), int(px + mx), int(py + my))]
                lines = await asyncio.to_thread(read_unread, shot, lines, cfg, spot)
            except Exception as err:  # pylint: disable=broad-exception-caught
                raise _ArgError(f"Commitment block: OCR check failed ({err}); tap refused.") from err
            for res in _merge_ocr_words(lines):
                box = _ocr_box(res)
                text = str(res.get("text") or "")
                if box and box[0] - mx <= px <= box[2] + mx and box[1] - my <= py <= box[3] + my:
                    if is_commitment_text(text):
                        raise _ArgError(
                            f"Commitment block: the screen shows '{text}' at {list(point)}."
                            " Commitment actions (placing an order, paying) are disabled for this run;"
                            " stop here and report the task status instead."
                        )

    async def _guard_morphed_commitment(
        self,
        session: Any,
        point: Any,
        state: Any,
        index_elements: list[dict[str, Any]] | None,
    ) -> None:
        """Refuses a point tap whose target became a commitment control.

        Checks the live hierarchy (one fetch, ~0.1-0.3 s; only for point actions
        after the first device action of a burst). When the check cannot run,
        the tap proceeds: this guard narrows a risk, it is not a gate.
        """
        from artemis.utils.screen_diff import COMMITMENT_PATTERN

        if session is None or not hasattr(session, "ui_hierarchy"):
            return
        try:
            live = await session.ui_hierarchy(timeout=2.0)
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.debug(f"Morphed-commitment check skipped: {err}")
            return
        live_label = self._label_at(live, point)
        if not live_label or not COMMITMENT_PATTERN.search(live_label):
            return
        seen_label = self._label_at(self._index_elements(state, index_elements), point)
        if seen_label == live_label:
            return  # the model saw this very control and chose it
        raise _ArgError(
            f"Commitment Guard: the control at {point} is now '{live_label}' (it was "
            f"'{seen_label or 'something else'}' on the screen you decided on). The screen changed "
            f"during this burst; look at the new screen before tapping a commitment control."
        )

    @staticmethod
    def _index_elements(
        state: Any, index_elements: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        """The element list index targets resolve against (``None`` -> state)."""
        if index_elements is not None:
            return index_elements
        return getattr(state, "indexed_elements", None) or []

    def _translate_swipe(
        self,
        args: dict[str, Any],
        state: Any,
        *,
        index_elements: list[dict[str, Any]] | None = None,
    ) -> tuple[str, dict[str, Any], Any, dict[str, Any]]:
        width, height = self._screen_size()
        default_duration = args.get("duration", 400)
        kind, target, final_duration = parse_swipe_parameters(
            dict(args), default_duration=default_duration
        )

        if kind == "direction":
            dir_name = target
            x1, y1, x2, y2, smart_dur = compute_smart_swipe_coordinates(
                direction=target,
                target=args.get("target"),
                indexed_elements=self._index_elements(state, index_elements) if state else None,
                ui_hierarchy=getattr(state, "latest_ui_hierarchy", None) if state else None,
                width=width,
                height=height,
                duration=final_duration,
            )

            def _norm(v: float, size: int) -> int:
                return int(max(0, min(1000, round(v * 1000 / max(1, size)))))

            def finalize(res: ActionResult) -> str:
                if not res.ok:
                    return f"Error swiping {dir_name}: {res.detail}"
                return f"Swiped {dir_name}."

            return (
                "swipe",
                {
                    "start": [_norm(x1, width), _norm(y1, height)],
                    "end": [_norm(x2, width), _norm(y2, height)],
                    "duration_ms": smart_dur,
                },
                finalize,
                {},
            )

        if kind == "coords":
            x1, y1, x2, y2 = target
            recorded = self._require_description(args, "swipe")

            def finalize(res: ActionResult) -> str:
                if not res.ok:
                    return f"Error dragging: {res.detail}"
                return f"Swiped from [{x1}, {y1}] to [{x2}, {y2}] (normalized)."

            return (
                "swipe",
                {
                    "start": [int(x1), int(y1)],
                    "end": [int(x2), int(y2)],
                    "duration_ms": final_duration,
                },
                finalize,
                recorded,
            )

        raise _ArgError(f"Error during swipe: Invalid direction: {args}")

    # --- Non-device tools (stay agent-side) ------------------------------------------

    async def _execute_agent_tool(
        self,
        raw_name: str,
        name: str,
        args: dict[str, Any],
        tool_call_id: str,
        state: Any,
    ) -> ToolExecutionResult:
        span = TraceSpan(name=raw_name, trace_type="tool", ctx=self.ctx)
        span.payload = {"args": args}
        ok: bool | None = None
        blocks: list[dict[str, Any]] | None = None
        with span:
            if raw_name == "read_note":
                text = await self._read_note(
                    args.get("key", ""), args.get("start_line"), args.get("end_line")
                )
            elif raw_name == "list_notes":
                text = await self._list_notes()
            elif raw_name == "video_analyzer":
                text, ok = await self._video_analyzer(
                    args.get("time_description") or args.get("TimeDescription") or "",
                    args.get("purpose") or args.get("Purpose") or "",
                )
            elif raw_name in HISTORY_TOOL_NAMES:
                text, blocks = await self._history_tool(raw_name, args)
            else:
                text, ok = await self._ask_explorer(
                    # ``task_description`` is the pre-contract argument name
                    # kept for old MCP clients.
                    args.get("query") or args.get("task_description") or "",
                    args.get("context_feedback"),
                    state,
                )
            span.result = {"outcome": text}
            if ok is False:
                span.status = "failed"
                span.error = text

        # The Explorer and the video analyzer report their status explicitly;
        # note and history texts carry it structurally (a ``ToolFailure``), so a
        # history lookup's "no match" answer is an answer, not an error.
        if ok is None:
            ok = not is_tool_failure(text)
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            tool_name=name,
            status="success" if ok else "error",
            text_summary=text,
            # Content blocks (a step screenshot) ride along for callers that
            # forward multimodal tool results; the text summary stays complete.
            raw_result=blocks,
        )

    async def _read_note(self, key: str, start_line: int | None, end_line: int | None) -> str:
        try:
            base_dir = self.ctx.data_engine.base_dir if self.ctx.data_engine else None
            if not base_dir:
                return format_read_note_failure(key, "DataEngine not initialized.")
            return read_note_content(base_dir, key, start_line, end_line)
        except Exception as e:
            return format_read_note_failure(key, str(e))

    async def _list_notes(self) -> str:
        try:
            base_dir = self.ctx.data_engine.base_dir if self.ctx.data_engine else None
            if not base_dir:
                return format_list_notes_failure("DataEngine not initialized.")
            return format_list_notes_success(list_notes_info(base_dir))
        except Exception as e:
            return format_list_notes_failure(str(e))

    async def _history_tool(
        self, raw_name: str, args: dict[str, Any]
    ) -> tuple[str, list[dict] | None]:
        """Runs one of the shared history tools (the same instances the Pro
        agents bind), dispatched generically by name.

        Returns the text answer and, for a screenshot, the multimodal content
        blocks the tool produced.
        """
        tool = history_tool_by_name(raw_name)
        if tool is None:
            return ToolFailure(f"Error: Tool '{raw_name}' not supported."), None
        accepted = {k: v for k, v in args.items() if k in tool.args_schema.model_fields}
        try:
            result = await tool.execute(ctx=self.ctx, **accepted)
        except Exception as e:
            logger.error(f"Error running {raw_name}: {e}")
            return ToolFailure(f"{raw_name} failed: {e}"), None
        text, images = split_multimodal_result(result)
        if images:
            return text or "Screenshot attached.", result
        return text, None

    async def _video_analyzer(self, time_description: str, purpose: str) -> tuple[str, bool]:
        """Runs the video-analyzing subagent over the session recording.

        Same subagent the Pro operator's ``video_analyzer`` LangChain tool
        delegates to; imported lazily because the video stack is heavy.
        """
        from artemis.agents.video_analyzer.video_analyzer import VideoAnalyzer

        try:
            outcome, status = await VideoAnalyzer(self.ctx).run(time_description, purpose)
        except Exception as e:
            logger.error(f"Error running video analyzer: {e}")
            return f"Error running video analyzer: {e}", False
        if status in ("failed", "error"):
            return f"Video analysis failed: {outcome}", False
        return outcome, True

    async def _ask_explorer(
        self, query: str, context_feedback: str | None, state: Any
    ) -> tuple[str, bool]:
        """Runs the Explorer pipeline for this executor's agent; returns ``(text, ok)``.

        ``ok`` is False only when the Explorer run itself failed: a clean
        "not found" is a successful answer the agent has to reason about.
        """
        # Imported lazily: the explorer stack is heavy and recursive (it spawns an LLM
        # sub-agent), which is also why it must never live behind the action server.
        from artemis.tools.explorer_tool import locate, register_candidates, render_text

        try:
            outcome = await locate(
                self.ctx, state, query, context_feedback or "", agent_name=self.agent_name
            )
            registered = register_candidates(self.ctx, state, outcome)
            # Candidates join ``state.indexed_elements``, and this executor's
            # ``click`` resolves an index against that list (Pro parity), so the
            # answer teaches the index syntax next to the coordinates.
            return render_text(query, outcome, registered, index_targets=True), not outcome.error
        except Exception as e:
            return f"Error executing ask_explorer: {e}", False
