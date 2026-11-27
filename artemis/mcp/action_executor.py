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
import difflib
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
        # Values typed by input_text in this run: never overwritten by a typing
        # aimed at a point that is not a field (L-029).
        self._typed_values: list[str] = []
        # Unfixed input_text value mismatches (L-051): field_key -> {actual, expected, label}.
        self._unfixed_typed_mismatches: dict[str, dict[str, str]] = {}
        # Normalized actual text seen on prior mismatch per field (L-055).
        self._typed_mismatch_history: dict[str, str] = {}
        # One-shot guard armed after workflow reaches end screen (L-039).
        self._workflow_end_guard: dict[str, Any] | None = None
        self._last_elements_signature: str | None = None
        self._last_image_hash: str | None = None
        # Observed scroll boundary after a blocked directional swipe: (edge, elements_sig) (L-060).
        self._scroll_edge_hint: tuple[str, str] | None = None
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
        recovered_screen_changed = False
        recovered_pre_observation: dict[str, Any] | None = None
        typed_value_mismatch: dict[str, str] | None = None
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
                target_coordinates = wire_args.get("target") if raw_name in _POINT_TARGET_ACTIONS else None
                self._check_workflow_end_guard(
                    raw_name, wire_args, target_semantics, target_coordinates
                )
                if raw_name in _POINT_TARGET_ACTIONS:
                    # The normalized point the action was sent to: the recorded
                    # coordinates of an index target (the model named an index,
                    # not a point).
                    if (
                        raw_name == "input_text"
                        and target_coordinates
                        and self.agent_name == "flash"
                    ):
                        non_field_err = self._non_input_target_error(
                            args, target_coordinates, state, index_elements
                        )
                        if non_field_err is not None:
                            raise _ArgError(non_field_err)
                        other = self._input_would_go_elsewhere(target_coordinates)
                        if other is not None:
                            recovery = await self._recover_keyboard_hidden_field(
                                args, session, state, index_elements=index_elements
                            )
                            if recovery is not None:
                                recovered_screen_changed = bool(recovery.get("screen_changed"))
                                rec_obs = recovery.get("obs")
                                if rec_obs is not None and getattr(rec_obs, "ok", False):
                                    shot_path = getattr(rec_obs, "screenshot_path", None) or shot_path
                                    xml_list = getattr(rec_obs, "elements_text", None) or xml_list
                                    if recovery.get("obs_bytes") is not None:
                                        img_bytes = recovery["obs_bytes"]
                                if recovery.get("recovered"):
                                    target_coordinates = recovery["point"]
                                    wire_args["target"] = target_coordinates
                                    target_semantics = recovery["semantics"]
                                    recovered_pre_observation = recovery.get("pre_observation")
                                else:
                                    desc_hint = recovery.get("hint_or_label") or recovery.get("unique_rid") or "the target field"
                                    raise _ArgError(
                                        f"input_text refused: hid the keyboard with BACK, but field"
                                        f" {desc_hint!r} is still not uniquely visible on the current"
                                        f" screen (typing would go into focused field {other!r})."
                                        f" Scroll to reveal the field and use its new index."
                                    )
                            else:
                                raise _ArgError(
                                    f"input_text refused: {target_coordinates} is not on an input field of"
                                    f" the current screen (it may be hidden by the keyboard or come from an"
                                    f" older element list), so the text would go into the focused field"
                                    f" {other!r} and could overwrite it. Use the index of the field from the"
                                    f" current list, or hide the keyboard / scroll to show the field first."
                                )
                        clash = self._input_overwrites_typed_field(
                            target_coordinates, str(args.get("text") or "")
                        )
                        if clash is not None and args.get("clear_exist") is False and self._append_is_in_goal(
                            clash[1], str(args.get("text") or "")
                        ):
                            # Intentional append: held value + new text is what the task asks for (L-056).
                            clash = None
                        if clash is not None:
                            field_name, prev_val = clash
                            raise _ArgError(
                                f"input_text refused: field {field_name!r} already holds {prev_val!r}"
                                " typed earlier in this run, and the new text is not editing that value."
                                " To fill a different field, pass that field's index from the element list"
                                " (hide the keyboard or scroll first if it is not shown). To change this"
                                " field's value, type the whole new value with clear_exist=true."
                            )
                    if raw_name == "input_text" and not target_coordinates and self.agent_name == "flash":
                        other = self._input_would_go_elsewhere(None)
                        if other is not None:
                            raise _ArgError(
                                f"input_text refused: no field was given, so the text would go into the"
                                f" focused field {other!r}, which already holds a value typed earlier in"
                                f" this run, and would overwrite it. Pass the index of the field to fill"
                                f" (hide the keyboard / scroll first if it is not listed)."
                            )
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
                pre_input_field = (
                    self._locate_live_target_field(target_coordinates, target_semantics)
                    if raw_name == "input_text"
                    else None
                )
                pre_swipe_elements = (
                    list(self._index_elements(state, index_elements) or [])
                    if raw_name == "swipe"
                    else []
                )
                pre_swipe_direction = (
                    str(args.get("direction") or "").strip().lower()
                    if raw_name == "swipe"
                    else ""
                )
                pre_input_text = ""
                if pre_input_field is not None:
                    raw_pre_text = str(pre_input_field.get("text") or "").strip()
                    raw_pre_hint = str(pre_input_field.get("hint") or "").strip()
                    if raw_pre_text and not (raw_pre_hint and raw_pre_text.casefold() == raw_pre_hint.casefold()):
                        pre_input_text = raw_pre_text

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
                    if raw_name == "input_text":
                        typed_value = str(args.get("text") or "").strip().casefold()
                        if typed_value:
                            if not hasattr(self, "_typed_values"):
                                self._typed_values = []
                            self._typed_values.append(typed_value)
                            del self._typed_values[:-20]
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
                            if raw_name == "swipe" and res.ok:
                                message = self._apply_swipe_outcome_feedback(
                                    base_message=finalize(res) if finalize else res.message,
                                    pre_elements=pre_swipe_elements,
                                    post_elements=obs.elements,
                                    direction=pre_swipe_direction,
                                    screen_unchanged=screen_unchanged,
                                    scrollable=obs.scrollable,
                                    curr_sig=curr_sig,
                                )
                            elif not screen_unchanged or obs.scrollable is False:
                                self._scroll_edge_hint = None
                            if raw_name == "input_text" and res.ok:
                                mismatch = self._typed_field_mismatch(
                                    target_coordinates,
                                    target_semantics,
                                    str(args.get("text") or ""),
                                    pre_text=pre_input_text,
                                    clear_exist=args.get("clear_exist"),
                                )
                                if mismatch is not None:
                                    verdict, actual_val, expected_val, field_key, field_label, is_pw = mismatch
                                    if verdict == "fail":
                                        typed_value_mismatch = {
                                            "actual": actual_val,
                                            "expected": expected_val,
                                        }
                                        if not hasattr(self, "_unfixed_typed_mismatches"):
                                            self._unfixed_typed_mismatches = {}
                                        self._unfixed_typed_mismatches[field_key] = {
                                            "actual": actual_val,
                                            "expected": expected_val,
                                            "label": field_label,
                                        }
                                        if not hasattr(self, "_typed_mismatch_history"):
                                            self._typed_mismatch_history = {}
                                        from artemis.workflow.targets import normalize_field_value

                                        self._typed_mismatch_history[field_key] = normalize_field_value(actual_val)
                                        clear_hint = (
                                            "Clear it (clear_exist=true) and type again, or explain why"
                                            f" {actual_val!r} is intended."
                                        )
                                        fail_msg = (
                                            f"input_text failed: password field holds {len(actual_val)} masked"
                                            f" characters, requested {expected_val}. {clear_hint}"
                                            if is_pw
                                            else f"input_text failed: field holds '{actual_val}', requested"
                                            f" '{expected_val}'. {clear_hint}"
                                        )
                                        res = ActionResult.failure(
                                            raw_name, fail_msg, code=ActionCode.INVALID_ARGS
                                        )
                                        message = fail_msg
                                    else:
                                        self._clear_fixed_typed_mismatch(
                                            target_coordinates,
                                            target_semantics,
                                            str(args.get("text") or ""),
                                        )
                                        note = (
                                            f" Note: field holds '{actual_val}'"
                                            f" (app formatted; requested '{expected_val}')."
                                        )
                                        message += note
                                        res = res.model_copy(update={"message": res.message + note})
                                else:
                                    self._clear_fixed_typed_mismatch(
                                        target_coordinates,
                                        target_semantics,
                                        str(args.get("text") or ""),
                                    )
                                    post_field = self._locate_live_target_field(
                                        target_coordinates, target_semantics
                                    )
                                    if post_field is not None and not post_field.get("password"):
                                        post_val = str(post_field.get("text") or "").strip().casefold()
                                        if post_val and hasattr(self, "_typed_values") and post_val not in self._typed_values:
                                            self._typed_values.append(post_val)
                                            del self._typed_values[:-20]

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
                **({"recovered_screen_changed": True} if recovered_screen_changed else {}),
                **({"recovered_pre_observation": recovered_pre_observation} if recovered_pre_observation else {}),
                **({"typed_value_mismatch": typed_value_mismatch} if typed_value_mismatch else {}),
            },
        )

    def arm_workflow_end_guard(self, targets: list[dict[str, Any]], summary: str) -> None:
        """Arms a one-shot block for entry/restart steps after a workflow reaches
        its recorded end screen (L-039)."""
        if targets:
            self._workflow_end_guard = {"targets": list(targets), "summary": str(summary or "").strip()}

    def _check_workflow_end_guard(
        self,
        raw_name: str,
        wire_args: dict[str, Any],
        target_semantics: dict[str, Any],
        target_coordinates: Any,
    ) -> None:
        guard = getattr(self, "_workflow_end_guard", None)
        if not guard:
            return
        targets = guard.get("targets") or []
        matched = False
        if raw_name in ("click", "tap", "long_press"):
            from artemis.workflow.targets import fold

            rid = str((target_semantics or {}).get("target_resource_id") or "").strip()
            txt = str((target_semantics or {}).get("target_text") or "").strip()
            tclass = str((target_semantics or {}).get("target_class") or "").strip()
            for gt in targets:
                if gt.get("tool") not in ("click", "tap", "long_press"):
                    continue
                g_rid = str(gt.get("resource_id") or "").strip()
                g_txt = str(gt.get("text") or "").strip()
                g_box = gt.get("bounds")
                if g_rid and rid == g_rid:
                    if not g_txt or not txt or fold(txt) == fold(g_txt):
                        matched = True
                        break
                if g_txt and txt and fold(txt) == fold(g_txt):
                    matched = True
                    break
                if (
                    isinstance(target_coordinates, (list, tuple))
                    and len(target_coordinates) == 2
                    and isinstance(g_box, (list, tuple))
                    and len(g_box) == 4
                ):
                    px, py = int(target_coordinates[0]), int(target_coordinates[1])
                    if g_box[0] - 15 <= px <= g_box[2] + 15 and g_box[1] - 15 <= py <= g_box[3] + 15:
                        matched = True
                        break
                if gt.get("class_name") and tclass == gt["class_name"] and isinstance(gt.get("grid"), (list, tuple)):
                    tbounds = (target_semantics or {}).get("target_bounds")
                    if isinstance(tbounds, (list, tuple)) and len(tbounds) == 4:
                        cx = (int(tbounds[0]) + int(tbounds[2])) // 2
                        cy = (int(tbounds[1]) + int(tbounds[3])) // 2
                        if (min(9, max(0, cx // 100)), min(9, max(0, cy // 100))) == tuple(gt["grid"]):
                            matched = True
                            break
        elif raw_name == "press_key":
            k = str(wire_args.get("key") or "").strip().upper()
            matched = any(gt.get("tool") == "press_key" and str(gt.get("key") or "").upper() == k for gt in targets)
        if matched:
            self._workflow_end_guard = None  # one-shot: a genuine retry passes next time
            summary = guard.get("summary") or "Workflow already reached its recorded end screen."
            raise _ArgError(
                f"Action refused once: {summary} Check the current screen first — if you truly need"
                " to repeat the workflow or add another item, retry this action."
            )

    def _matching_typed_value(self, held: str) -> str | None:
        """Returns the session-typed value (len >= 2) held by a field, else None."""
        if not held:
            return None
        typed = [v for v in getattr(self, "_typed_values", []) if len(v) >= 2]
        for v in typed:
            if held == v or bool(re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", held)):
                return v
        return None

    def _is_session_typed_text(self, text: str) -> bool:
        """True when ``text`` matches a value typed earlier by this run (L-055)."""
        raw = str(text or "").strip()
        if not raw:
            return False
        from artemis.workflow.targets import normalize_field_value

        target_norm = normalize_field_value(raw)
        typed = [str(v or "") for v in getattr(self, "_typed_values", []) if str(v or "").strip()]
        if any(normalize_field_value(v) == target_norm for v in typed):
            return True
        return self._matching_typed_value(raw.casefold()) is not None

    def _normalized_fields(self) -> list[dict[str, Any]]:
        raw = getattr(self.ctx, "last_raw_observation", None)
        if not isinstance(raw, dict) or not raw.get("elements"):
            return []
        try:
            from artemis.workflow.fingerprint import normalize
            from artemis.workflow.targets import is_editable, visible_app_nodes

            norm = normalize(raw["elements"], width=raw.get("width"), height=raw.get("height"))
        except Exception:
            return []
        return [n for n in visible_app_nodes(norm) if is_editable(n)]

    def _locate_live_target_field(
        self,
        target_point: list[int] | tuple[int, int] | None,
        target_semantics: dict[str, Any],
    ) -> dict[str, Any] | None:
        fields = self._normalized_fields()
        if not fields:
            return None
        from artemis.workflow.targets import fold, node_label

        if target_point is not None:
            x, y = int(target_point[0]), int(target_point[1])
            margin = 10
            hits = [
                f
                for f in fields
                if f["bounds"][0] - margin <= x <= f["bounds"][2] + margin
                and f["bounds"][1] - margin <= y <= f["bounds"][3] + margin
            ]
            if hits:
                return min(
                    hits,
                    key=lambda f: (f["bounds"][2] - f["bounds"][0]) * (f["bounds"][3] - f["bounds"][1]),
                )
        if isinstance(target_semantics, dict):
            rid = str(target_semantics.get("target_resource_id") or "").strip()
            if rid:
                rid_hits = [f for f in fields if str(f.get("resource-id") or "").strip() == rid]
                if len(rid_hits) == 1:
                    return rid_hits[0]
            hint_or_txt = fold(str(target_semantics.get("target_text") or ""))
            if hint_or_txt:
                label_hits = [
                    f
                    for f in fields
                    if fold(str(f.get("hint") or "")) == hint_or_txt or fold(node_label(f)) == hint_or_txt
                ]
                if len(label_hits) == 1:
                    return label_hits[0]
        focused = [f for f in fields if f.get("focused")]
        return focused[0] if len(focused) == 1 else None

    @staticmethod
    def _field_mismatch_key(
        target_field: dict[str, Any] | None,
        target_point: list[int] | tuple[int, int] | None,
        target_semantics: dict[str, Any],
    ) -> tuple[str, str]:
        from artemis.workflow.targets import node_label

        if target_field is not None:
            rid = str(target_field.get("resource-id") or "").strip()
            hint = str(target_field.get("hint") or "").strip()
            label = hint or node_label(target_field) or rid or "input field"
            key = f"rid:{rid}|hint:{hint}" if (rid or hint) else f"bounds:{tuple(target_field.get('bounds') or ())}"
            return key, label
        rid_s = str((target_semantics or {}).get("target_resource_id") or "").strip()
        txt_s = str((target_semantics or {}).get("target_text") or "").strip()
        key = f"rid:{rid_s}|hint:{txt_s}" if (rid_s or txt_s) else f"pt:{tuple(target_point or ())}"
        return key, (txt_s or rid_s or "input field")

    def _clear_fixed_typed_mismatch(
        self,
        target_point: list[int] | tuple[int, int] | None,
        target_semantics: dict[str, Any],
        expected_text: str,
    ) -> None:
        mismatches = getattr(self, "_unfixed_typed_mismatches", None)
        if not mismatches:
            return
        field = self._locate_live_target_field(target_point, target_semantics)
        field_key, _ = self._field_mismatch_key(field, target_point, target_semantics)
        mismatches.pop(field_key, None)
        exp = str(expected_text or "").strip()
        if exp:
            for k in [k for k, v in mismatches.items() if v.get("expected") == exp]:
                mismatches.pop(k, None)

    def scroll_edge_state(self) -> str | None:
        """Returns ``'top'`` or ``'bottom'`` when a directional swipe on the
        current screen hit no visible change (L-060), else ``None``."""
        hint = getattr(self, "_scroll_edge_hint", None)
        if not isinstance(hint, tuple) or len(hint) != 2:
            return None
        edge, sig = hint
        if sig and self._last_elements_signature and sig != self._last_elements_signature:
            return None
        return edge if edge in ("top", "bottom") else None

    @staticmethod
    def _swipe_element_labels(elements: list[dict[str, Any]] | None) -> tuple[list[tuple[str, str]], set[str]]:
        """Extracts deduplicated ``(key, display)`` pairs for UI diffing across a swipe."""
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for el in elements or ():
            if not isinstance(el, dict) or el.get("is_keyboard"):
                continue
            txt = str(el.get("text") or el.get("hint") or el.get("nearby_label") or "").strip()
            if txt:
                key = f"txt:{txt.casefold()}"
                disp = '"' + txt[:40].replace('"', "'") + '"'
            elif el.get("is_icon"):
                cls = str(el.get("class") or "")
                kind = (
                    "input field"
                    if ("EditText" in cls or str(el.get("editable") or "").lower() == "true")
                    else ("icon" if "Image" in cls else "button")
                )
                rid = str(el.get("resource_id") or "").strip()
                key = f"icon:{kind}:{rid or tuple(el.get('bounds') or ())}"
                disp = f"({kind})"
            else:
                continue
            if key in seen:
                continue
            seen.add(key)
            pairs.append((key, disp))
        return pairs, seen

    def _apply_swipe_outcome_feedback(
        self,
        *,
        base_message: str,
        pre_elements: list[dict[str, Any]],
        post_elements: list[dict[str, Any]],
        direction: str,
        screen_unchanged: bool,
        scrollable: bool | None,
        curr_sig: str,
    ) -> str:
        """Formats clear finger-vs-page swipe movement, New/Hidden elements, and
        edge hints when a directional swipe had no visible effect (L-060)."""
        if screen_unchanged:
            if not direction:
                return f"{base_message} (no visible change)"
            if scrollable is False:
                self._scroll_edge_hint = None
                return f"No visible change after swiping {direction}: this screen has no scrollable container."
            if direction == "down":
                self._scroll_edge_hint = ("top", curr_sig)
                return (
                    "No visible change after swiping down (finger down): already at the TOP edge — "
                    "content further DOWN needs direction='up'."
                )
            if direction == "up":
                self._scroll_edge_hint = ("bottom", curr_sig)
                return (
                    "No visible change after swiping up (finger up): already at the BOTTOM edge — "
                    "content further UP needs direction='down'."
                )
            self._scroll_edge_hint = None
            opposite = "right" if direction == "left" else ("left" if direction == "right" else "")
            hint = f" try direction='{opposite}'." if opposite else ""
            return f"No visible change after swiping {direction} (finger {direction}): already at that edge.{hint}"

        self._scroll_edge_hint = None
        dir_lead = {
            "down": "Swiped down (finger down): the page moved toward its TOP.",
            "up": "Swiped up (finger up): the page moved toward its BOTTOM.",
            "left": "Swiped left (finger left): the view scrolled RIGHT.",
            "right": "Swiped right (finger right): the view scrolled LEFT.",
        }.get(direction, base_message)

        pre_pairs, pre_keys = self._swipe_element_labels(pre_elements)
        post_pairs, post_keys = self._swipe_element_labels(post_elements)
        new_items = [disp for k, disp in post_pairs if k not in pre_keys][:6]
        hidden_items = [disp for k, disp in pre_pairs if k not in post_keys][:6]

        parts = [dir_lead]
        if new_items:
            parts.append("New: " + ", ".join(new_items) + ".")
        if hidden_items:
            parts.append("Hidden: " + ", ".join(hidden_items) + ".")
        return " ".join(parts)

    def consume_unfixed_typed_mismatches(self) -> list[dict[str, str]]:
        """Returns any unfixed typed-value mismatches once and clears them (L-051)."""
        mismatches = getattr(self, "_unfixed_typed_mismatches", None)
        if not mismatches:
            return []
        items = list(mismatches.values())
        mismatches.clear()
        return items

    def _non_input_target_error(
        self,
        args: dict[str, Any],
        target_point: list[int] | tuple[int, int],
        state: Any,
        index_elements: list[dict[str, Any]] | None,
    ) -> str | None:
        """Refuses ``input_text(target=i)`` when element ``i`` is clearly a non-input
        control (e.g. a button/icon) and no editable input field covers that point (L-050)."""
        if args.get("target_text"):
            return None
        raw_target = args.get("target") or args.get("coordinates")
        norm_target = normalize_coordinate_target(raw_target)
        if isinstance(norm_target, bool) or not isinstance(norm_target, (int, float)):
            return None
        elements = self._index_elements(state, index_elements)
        idx = int(norm_target)
        if not 1 <= idx <= len(elements):
            return None
        el = elements[idx - 1]
        if el.get("is_ocr") or el.get("is_hint") or el.get("hint"):
            return None
        cls = str(el.get("class") or "")
        editable_flag = str(el.get("editable") or "").strip().lower() == "true" or bool(el.get("editable") is True)
        if "EditText" in cls or "AutoCompleteTextView" in cls or editable_flag:
            return None
        x, y = int(target_point[0]), int(target_point[1])
        margin = 10
        for f in self._normalized_fields():
            l, t, r, b = f["bounds"]
            if l - margin <= x <= r + margin and t - margin <= y <= b + margin:
                return None
        short_cls = cls.rsplit(".", 1)[-1] if cls else ""
        if el.get("is_icon"):
            if "Image" in short_cls:
                role = "icon"
            elif any(k in short_cls for k in ("Switch", "CheckBox", "RadioButton", "ToggleButton")):
                role = "toggle"
            else:
                role = "button"
        else:
            role = short_cls or "element"
        lbl = str(el.get("text") or "").strip()
        desc = f"{lbl!r} ({role})" if lbl else f"({role})"
        return (
            f"input_text refused: element [{idx}] {desc} is not an input field, "
            "and no input field contains that point (typing at it would tap a control). "
            "Pass the index of an input field, or if the cursor is already blinking inside the "
            "target field, call input_text(text=...) without target."
        )

    def _typed_field_mismatch(
        self,
        target_point: list[int] | tuple[int, int] | None,
        target_semantics: dict[str, Any],
        expected_text: str,
        *,
        pre_text: str = "",
        clear_exist: Any = None,
    ) -> tuple[str, str, str, str, str, bool] | None:
        """After ``input_text``, classifies any value difference in the live target
        field (L-045, L-049, L-051, L-055):

        Returns ``(verdict, actual, expected, field_key, field_label, is_pw)`` or
        ``None`` when matched/unverifiable/intentional append:
        - Intentional append (``clear_exist=False`` on session-typed text): ``None``.
        - ``verdict == "fail"`` only for untyped prefill concatenation
          (``actual == pre_text + expected``, e.g. ``+1``), accent drops, or
          password mask length mismatch — and never when ``clear_exist=True``
          already repeated the exact same mismatch (app auto-formatting -> ``"warn"``).
        """
        raw_expected = str(expected_text or "")
        expected = raw_expected.strip()
        if not expected:
            return None
        target_field = self._locate_live_target_field(target_point, target_semantics)
        if target_field is None:
            return None
        from artemis.workflow.targets import check_field_value, fold, normalize_field_value

        matched, verifiable, actual = check_field_value(target_field, expected)
        if not verifiable or matched:
            return None
        # Rule 2 (L-055): intentional append onto text typed earlier in this run.
        if clear_exist is False and pre_text and self._is_session_typed_text(pre_text):
            if (
                check_field_value(target_field, f"{pre_text}{raw_expected}")[0]
                or check_field_value(target_field, f"{pre_text} {expected}")[0]
            ):
                return None

        field_key, field_label = self._field_mismatch_key(target_field, target_point, target_semantics)
        is_pw = bool(target_field.get("password"))
        actual_norm = normalize_field_value(actual)
        exp_norm = normalize_field_value(expected)
        prev_actual_norm = getattr(self, "_typed_mismatch_history", {}).get(field_key)

        is_untyped_prefill_concat = bool(
            not is_pw
            and pre_text
            and not self._is_session_typed_text(pre_text)
            and actual_norm in (
                normalize_field_value(f"{pre_text}{raw_expected}"),
                normalize_field_value(f"{expected}{pre_text}"),
            )
        )
        is_accent_drop = bool(
            not is_pw and fold(actual) == fold(expected) and actual_norm != exp_norm
        )

        if clear_exist is True and prev_actual_norm is not None and prev_actual_norm == actual_norm:
            verdict = "warn"
        elif is_untyped_prefill_concat or is_accent_drop or is_pw:
            verdict = "fail"
        else:
            verdict = "warn"

        if is_pw:
            return verdict, actual, f"{len(expected)} masked characters", field_key, field_label, True
        return verdict, actual, expected, field_key, field_label, False

    def _input_would_go_elsewhere(self, target_point: list[int] | tuple[int, int] | None) -> str | None:
        """Before ``input_text`` aimed at ``target_point`` (0-1000), on the screen
        the model is looking at: the label of the field the text would really go
        into, when the point is not on any input field while another field holds
        focus AND holds a value typed earlier in this run (target hidden by the
        keyboard, an index from an older list): typing goes to the focused field
        and would overwrite it (L-029). Empty fields and content this run did not
        type (a URL to replace) are allowed. None otherwise.
        """
        fields = self._normalized_fields()
        if not fields:
            return None
        from artemis.workflow.targets import node_label

        focused = [n for n in fields if n.get("focused")]
        if len(focused) != 1:
            return None
        if target_point is not None:
            x, y = int(target_point[0]), int(target_point[1])
            margin = 10
            for f in fields:
                l, t, r, b = f["bounds"]
                if l - margin <= x <= r + margin and t - margin <= y <= b + margin:
                    return None  # the point is on an input field: the tap focuses it
        # target_point None: input_text with no target types into the focused field
        f = focused[0]
        held = str(f.get("text") or "").strip().casefold()
        if self._matching_typed_value(held) is None:
            # Empty, or content this run did not type (a URL, an old query): the
            # model means to replace it; only our own earlier input is protected.
            return None
        return str(f.get("hint") or node_label(f) or f.get("resource-id") or "the focused field")

    @staticmethod
    def _intended_field_descriptor(
        index: int, elements_list: list[dict[str, Any]]
    ) -> tuple[str, str] | None:
        """Identifies an index-targeted field from its pre-keyboard list (L-043).

        ``resource_id`` is only trusted when it appears once in ``elements_list``;
        form inputs commonly share a generic resource-id, in which case we rely
        on the field's hint or text label instead.
        """
        if not 1 <= index <= len(elements_list):
            return None
        el = elements_list[index - 1]
        rid = str(el.get("resource_id") or el.get("resource-id") or "").strip()
        unique_rid = ""
        if rid:
            rid_count = sum(
                1
                for item in elements_list
                if str(item.get("resource_id") or item.get("resource-id") or "").strip() == rid
            )
            if rid_count == 1:
                unique_rid = rid
        hint_or_label = str(el.get("hint") or el.get("text") or "").strip()
        if not unique_rid and not hint_or_label:
            return None
        return unique_rid, hint_or_label

    async def _recover_keyboard_hidden_field(
        self,
        args: dict[str, Any],
        session: Any,
        state: Any,
        *,
        index_elements: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Dismisses the soft keyboard once via BACK when an indexed input field
        disappeared off-screen, then re-locates the field on the live screen (L-043).

        Never swipes. Only acts when the target was an element index and a soft
        keyboard is currently open; otherwise returns ``None`` so L-029 refuses
        immediately.
        """
        if args.get("target_text"):
            return None
        raw_target = args.get("target") or args.get("coordinates")
        norm_target = normalize_coordinate_target(raw_target)
        if isinstance(norm_target, bool) or not isinstance(norm_target, (int, float)):
            return None
        elements_list = self._index_elements(state, index_elements)
        ident = self._intended_field_descriptor(int(norm_target), elements_list)
        if ident is None:
            return None
        unique_rid, hint_or_label = ident

        raw_before = getattr(self.ctx, "last_raw_observation", None)
        if not isinstance(raw_before, dict) or not raw_before.get("elements"):
            return None

        from artemis.workflow.fingerprint import normalize
        from artemis.workflow.targets import (
            center,
            covered_by_other_window,
            fold,
            is_editable,
            is_occluded,
            keyboard_box,
            keyboard_covers,
            node_label,
            visible_app_nodes,
        )

        def _find_unique_field(norm_screen: list[dict[str, Any]]) -> dict[str, Any] | None:
            visible = visible_app_nodes(norm_screen)
            candidates = [
                n
                for n in visible
                if is_editable(n)
                and not is_occluded(n, visible)
                and not covered_by_other_window(n, norm_screen)
                and not keyboard_covers(n, norm_screen)
            ]
            matches: list[dict[str, Any]] = []
            if unique_rid:
                matches = [
                    n
                    for n in candidates
                    if str(n.get("resource-id") or "").strip() == unique_rid
                ]
            if not matches and hint_or_label:
                want = fold(hint_or_label)
                matches = [
                    n
                    for n in candidates
                    if fold(str(n.get("hint") or "")) == want or fold(node_label(n)) == want
                ]
            return matches[0] if len(matches) == 1 else None

        def _recovered_payload(
            node: dict[str, Any],
            *,
            screen_changed: bool,
            pre_obs: dict[str, Any] | None = None,
            obs: Any = None,
            obs_bytes: bytes | None = None,
        ) -> dict[str, Any]:
            b = [int(v) for v in node["bounds"]]
            cx, cy = center((b[0], b[1], b[2], b[3]))
            semantics = {
                "target_text": str(node.get("text") or node.get("hint") or ""),
                "target_bounds": b,
                "target_resource_id": node.get("resource-id"),
                "target_class": node.get("class"),
            }
            return {
                "recovered": True,
                "point": [cx, cy],
                "semantics": semantics,
                "pre_observation": pre_obs,
                "obs": obs,
                "obs_bytes": obs_bytes,
                "screen_changed": screen_changed,
            }

        try:
            norm_before = normalize(
                raw_before["elements"],
                width=raw_before.get("width"),
                height=raw_before.get("height"),
            )
        except Exception:
            return None

        # Step (2): Try finding the unique unoccluded field on the current screen first
        # (e.g., when auto-scroll shifted it without hiding it under the keyboard).
        direct = _find_unique_field(norm_before)
        if direct is not None:
            return _recovered_payload(direct, screen_changed=False)

        if keyboard_box(norm_before) is None:
            return None
        if session is None or not hasattr(session, "call") or not hasattr(session, "observe"):
            return None

        logger.info(
            "L-043: pressing BACK once to hide keyboard and re-locate input field "
            f"(unique_rid={unique_rid!r}, label={hint_or_label!r})"
        )
        back_res = await session.call("press_key", {"key": "BACK"})
        if not getattr(back_res, "ok", False):
            return None
        obs = await session.observe(settle_ms=300)
        if is_device_offline_result(obs):
            raise DeviceOfflineError(obs.message)

        obs_bytes: bytes | None = None
        if getattr(obs, "ok", False):
            shot_path = getattr(obs, "screenshot_path", None)
            if shot_path:
                try:
                    obs_bytes = Path(shot_path).read_bytes()
                except Exception as read_err:
                    logger.debug(f"L-043 post-BACK screenshot read skipped: {read_err}")
            if getattr(obs, "hierarchy_ok", False):
                if state is not None:
                    state.indexed_points = [el["center"] for el in obs.elements]
                    state.indexed_elements = obs.elements
                    state.screen_scrollable = obs.scrollable
                self.record_observation(obs.elements, obs_bytes)

        raw_after = getattr(self.ctx, "last_raw_observation", None)
        if not isinstance(raw_after, dict) or not raw_after.get("elements"):
            if getattr(obs, "hierarchy_ok", False) and getattr(obs, "elements", None):
                raw_after = {
                    "elements": obs.elements,
                    "width": getattr(obs, "width", None),
                    "height": getattr(obs, "height", None),
                }
            else:
                return {
                    "recovered": False,
                    "obs": obs,
                    "obs_bytes": obs_bytes,
                    "screen_changed": True,
                    "unique_rid": unique_rid,
                    "hint_or_label": hint_or_label,
                }

        try:
            norm_after = normalize(
                raw_after["elements"],
                width=raw_after.get("width"),
                height=raw_after.get("height"),
            )
        except Exception:
            return {
                "recovered": False,
                "obs": obs,
                "obs_bytes": obs_bytes,
                "screen_changed": True,
                "unique_rid": unique_rid,
                "hint_or_label": hint_or_label,
            }

        node = _find_unique_field(norm_after)
        if node is None:
            return {
                "recovered": False,
                "obs": obs,
                "obs_bytes": obs_bytes,
                "screen_changed": True,
                "unique_rid": unique_rid,
                "hint_or_label": hint_or_label,
            }

        pre_observation = {
            "elements": list(raw_after.get("elements") or []),
            "width": raw_after.get("width"),
            "height": raw_after.get("height"),
            "seq": raw_after.get("seq"),
        }
        return _recovered_payload(
            node,
            screen_changed=True,
            pre_obs=pre_observation,
            obs=obs,
            obs_bytes=obs_bytes,
        )

    def _append_is_in_goal(self, held: str, new_text: str) -> bool:
        """True when appending ``new_text`` to ``held`` gives text the task goal
        itself contains (spaces/case ignored): only then is a clear_exist=False
        typing onto a value typed earlier in this run an intentional append and
        not two separate values merged into one field ("Son" + "Dao") (L-056)."""
        goal = str(getattr(self, "task_goal", "") or "")
        if not goal:
            return False
        from artemis.workflow.targets import normalize_field_value

        combined = normalize_field_value(f"{held}{new_text}")
        return bool(combined) and combined in normalize_field_value(goal)

    def _input_overwrites_typed_field(
        self, target_point: list[int] | tuple[int, int], new_text: str
    ) -> tuple[str, str] | None:
        """Before ``input_text`` aimed at a real input field: refuses when that
        field already holds a value ``V`` typed earlier in this run and ``new_text``
        is unrelated to ``V`` (neither contains/is contained by ``V``, nor edits it
        with similarity >= 0.5) (L-037)."""
        nt = str(new_text or "").strip().casefold()
        if not nt:
            return None
        fields = self._normalized_fields()
        if not fields:
            return None
        from artemis.workflow.targets import fold, node_label

        x, y = int(target_point[0]), int(target_point[1])
        margin = 10
        hits = [
            f
            for f in fields
            if f["bounds"][0] - margin <= x <= f["bounds"][2] + margin
            and f["bounds"][1] - margin <= y <= f["bounds"][3] + margin
        ]
        if not hits:
            return None
        target_field = min(
            hits,
            key=lambda f: (f["bounds"][2] - f["bounds"][0]) * (f["bounds"][3] - f["bounds"][1]),
        )
        held_raw = str(target_field.get("text") or "").strip()
        hint_raw = str(target_field.get("hint") or "").strip()
        if not held_raw or (hint_raw and fold(held_raw) == fold(hint_raw)):
            return None
        matched = self._matching_typed_value(held_raw.casefold())
        if matched is None:
            return None
        v_fold = fold(matched)
        nt_fold = fold(nt)
        if not nt_fold or v_fold in nt_fold or nt_fold in v_fold:
            return None
        if difflib.SequenceMatcher(None, v_fold, nt_fold).ratio() >= 0.5:
            return None
        held_fold = fold(held_raw)
        if held_fold in nt_fold or nt_fold in held_fold:
            return None
        if difflib.SequenceMatcher(None, held_fold, nt_fold).ratio() >= 0.5:
            return None
        label = str(target_field.get("hint") or node_label(target_field) or target_field.get("resource-id") or "this field")
        return label, (held_raw if fold(held_raw) == v_fold else matched)

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
            raw_obs = getattr(self.ctx, "last_raw_observation", None)
            ui_tree = (getattr(state, "latest_ui_hierarchy", None) if state else None) or (
                raw_obs.get("elements") if isinstance(raw_obs, dict) else None
            )
            if isinstance(raw_obs, dict) and raw_obs.get("width") and raw_obs.get("height"):
                width = int(raw_obs["width"])
                height = int(raw_obs["height"])
            x1, y1, x2, y2, smart_dur = compute_smart_swipe_coordinates(
                direction=target,
                target=args.get("target"),
                indexed_elements=self._index_elements(state, index_elements) if state else None,
                ui_hierarchy=ui_tree,
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

            start = [_norm(x1, width), _norm(y1, height)]
            end = [_norm(x2, width), _norm(y2, height)]
            # The swipe actually sent (moved off floating buttons / the keyboard):
            # recorded on the step so the trace and the UI overlay show where it
            # really ran, not the generic direction default (L-030).
            resolved = {
                "coordinates": start + end,
                "normalized_coordinates": start + end,
                "normalized_start_coordinates": start,
                "normalized_end_coordinates": end,
            }
            return (
                "swipe",
                {
                    "start": start,
                    "end": end,
                    "duration_ms": smart_dur,
                },
                finalize,
                resolved,
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
