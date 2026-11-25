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

"""Universal Multi-Model FlashRunner for Artemis.

Executes autonomous reactive mobile workflows across Google Gemini,
OpenAI GPT-4o/o3, Anthropic Claude 3.5/3.7, and OpenRouter endpoints.

Conversation shape (shared with the Pro operator, history redesign §3.2):
the prompt is built every turn from a session :class:`TranscriptLedger` —
a byte-stable system prefix, the committed earlier turns (append-only; old UI
lists stripped at depth 1, screenshots resolved to visual summaries at depth
K, long spans chunk-compressed), and a fresh tail carrying the current
observation under a ``# CURRENT OBSERVATION [T+mm:ss]`` header. Actions run
synchronously and their tool messages already carry the outcome; only a turn
whose action failed ends with an ``--- Action Execution Result (T+mm:ss) ---``
message (it carries the error). Every timestamp the model sees is a
session-relative offset — the same clock the video analyzer uses for the
session recording.

Per-turn notices on the tail (the reasoning reminder after a silent turn, the
no-tool-call nudge, the final-turn warning, the user-guidance wrapper) are
marked ephemeral: the ledger's scrub edge deletes them once the message leaves
the recent window, so they never reach the frozen history or a chunk capsule.
The verbatim body of a user instruction is the exception: it stays in the
active window until its turn is chunked, whose user lines then carry it on.
"""

import asyncio
import base64
import time
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any
import uuid

from jinja2 import Template
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from artemis.core.tool_declaration import ToolDeclaration
from artemis.agents.flash.summarizer import VisualStepSummarizer, build_focus_context
from artemis.agents.operator.prompts import REASONING_REMINDER, UserGuidance, render_user_guidance
from artemis.agents.validator.tool_declarations import (
    ASK_EXPLORER_TOOL,
    CLICK_SEQUENCE_TOOL,
    LIST_NOTES_TOOL,
    READ_NOTE_TOOL,
    REPORT_TASK_STATUS_TOOL,
    VALIDATOR_TOOLS_DECLARATION,
    WAIT_FOR_SCREEN_CHANGE_TOOL,
    WAIT_FOR_TEXT_TOOL,
    WAIT_UNTIL_GONE_TOOL,
    capture_screenshot_and_parse_ui,
    prune_intermediate_screenshots,
)
from artemis.clients.screen_client_factory import DeviceOfflineError
from artemis.config import (
    MemoryRuntimeConfig,
    MemoryTranscriptConfig,
    StepSummarizerConfig,
    load_agent_config,
)
from artemis.config.agent import FlashModelImageConfig
from artemis.config.llm import get_default_llm_config
from artemis.utils import turn_log
from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.trace import trace
from artemis.graph.perception import _check_injected_instruction_file
from artemis.graph.state import State
from artemis.llm.structured import ParseFailure, parse_structured
from artemis.mcp.action_executor import McpActionExecutor
from artemis.mcp.action_types import is_device_offline_result
from artemis.mcp.screen_effect import NO_EFFECT_MARKER
from artemis.mcp.observation import observe
from artemis.memory.transcript import PRO_UI_LIST_MARKER, TranscriptLedger, mark_ephemeral
from artemis.services.llm import (
    LLMResumedError,
    RobustChatModelWrapper,
    acomplete,
    get_google_llm,
    get_llm,
    invoke_llm_with_timeout_message,
    reobserve_on_resume,
)
from artemis.tools.history import history_tool_declarations
from artemis.tools.tool_wrapper import tool_result_messages
from artemis.utils.model_image import shrink_for_model
from artemis.utils.screen_diff import COMMITMENT_PATTERN


def _has_stale_screen_target(name: str, args: dict) -> bool:
    """Checks whether an action specifies coordinate/index targets that belong to an earlier screen."""
    if args.get("target_text"):
        return False

    # 1. Any point target (index or [x, y])
    target = args.get("target")
    if target is not None:
        return True
    if args.get("coordinates") is not None:
        return True

    # 2. click_sequence uses explicit coordinates from old screen
    if name == "click_sequence":
        return True

    # 3. focus_and_clear_text requires coordinates
    if name == "focus_and_clear_text":
        return True

    # 4. swipe with explicit start/end coordinates (direction-only swipe is safe)
    if name == "swipe":
        if args.get("start") is not None or args.get("end") is not None:
            return True

    return False


from artemis.utils.ai_io_logger import (
    log_ai_input,
    log_ai_output,
    log_android_output,
    log_session_start,
)
from artemis.utils.coordinates import (
    COORDINATE_SPACE_KEY,
    COORDINATE_SPACE_NORMALIZED,
    compute_smart_swipe_coordinates,
    parse_swipe_parameters,
)
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

# One completion probe per task: once a deferred report has shown the model the
# rest of the page, a second probe swipe rarely reveals anything new and costs a
# full extra turn (~10-15 s observed on a lichess game page).
_MAX_SCROLL_PROBES = 1
_SCROLL_CHANGE_MIN_DISTANCE = 6
_MORE_BELOW_NOTICE = (
    "Report not accepted yet: this page had more content below. The system swiped up"
    " once and the screen changed, so part of the page was never reviewed. Look at the"
    " newly visible part (required choices, options, quantities, delivery, payment,"
    " promotions, notes, totals), keep scrolling to the end if needed, act on anything"
    " required, then report again with what you found and chose."
)

_RESUMED_NOTICE = (
    "--- Execution Resumed ---\n"
    "The task was paused (the AI provider failed) and has just been resumed. The"
    " device may have changed while it was paused: the app may have been closed or"
    " another screen opened. Judge the situation only from this fresh observation,"
    " not from the screen you saw before the pause. Your earlier history and"
    " progress still stand: if the app is no longer open, relaunch it and return to"
    " where you left off."
)

_NO_TOOL_CALL_NOTICE = (
    "You did not call any tools last turn. Please make progress by calling an"
    " action tool or 'report_task_status'."
)

_FINAL_TURN_WARNING = "[WARNING] This is your final turn; only 'report_task_status' is available."


@dataclass
class _TurnRecord:
    """What one reactive turn produced, for committing it into the ledger.

    ``step_keys`` are the DataEngine step ids recorded this turn (one per
    executed action; the tool_call_id stands in without a DataEngine) — the
    first keys the observation screenshot to its visual-transition summary,
    all of them feed the chunk ledger. ``actions`` collects the outcome of
    every device action so the turn's execution result can be rendered.
    """

    step_keys: list[str] = field(default_factory=list)
    actions: list[tuple[str, str, str]] = field(default_factory=list)

    def result(self) -> dict | None:
        """The turn's execution result in the validator-report shape.

        ``None`` unless an action failed: Flash executes synchronously and each
        action's tool message already carries its outcome, so a result message
        would only repeat it. A failure is worth its own message because it
        carries the error the turn ended on.
        """
        for name, status, text in self.actions:
            if status != "success":
                # Executor messages name the action themselves ("Error
                # executing click: ..."); prefix only the ones that don't.
                return {"status": "failed", "error": text if name in text else f"{name}: {text}"}
        return None


def _gemini_image_resolution(ctx: ArtemisContext) -> str | None:
    """'low' / 'medium' / 'high' when the operator node (the Flash model) is a
    Gemini model with ``media_resolution`` set; None otherwise."""
    try:
        llm_cfg = getattr(ctx, "llm_config", None) or get_default_llm_config()
        node = llm_cfg.get_agent("operator")
        level = getattr(node, "media_resolution", None)
        if level in ("low", "medium", "high") and str(getattr(node, "provider", "")) == "google":
            return level
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    return None



TEXT_ONLY_NOTE = (
    "--- No screenshot this turn (text mode): act from the element list. If the list is not "
    "enough (an unlabeled icon you cannot place, colors or a highlighted error, picture "
    "content, a control missing from the list), call look_at_screen to get the screenshot "
    "next turn. ---"
)

LOOK_AT_SCREEN_TOOL = ToolDeclaration(
    name="look_at_screen",
    description=(
        "Get the current screenshot in the next observation, when the element list alone "
        "is not enough to decide. Ends the turn; no device action."
    ),
    parameters={"type": "object", "properties": {}},
)


def _list_signature(xml_list: str | None) -> set[str]:
    """Element-list lines with indices and numbers removed."""
    if not xml_list or "Not available" in xml_list:
        return set()
    return {re.sub(r"\d+", "#", ln.strip()) for ln in xml_list.splitlines() if ln.strip()}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if (a or b) else 1.0


def _pixels_changed(before: bytes, after: bytes) -> bool:
    """64x64 grayscale comparison (status bar masked), as in wait_for_screen_change."""
    try:
        from io import BytesIO

        from PIL import Image

        from artemis.utils.screen_diff import compare_screen_pixels, mask_noise

        a = Image.open(BytesIO(before))
        b = Image.open(BytesIO(after))
        if a.size != b.size:
            return True
        a = mask_noise(a.convert("L"), None, a.width, a.height)
        b = mask_noise(b.convert("L"), None, b.width, b.height)
        changed, _cells = compare_screen_pixels(a, b, width=a.width, height=a.height)
        return changed
    except Exception:
        return True


class FlashRunner:
    """Reactive, ultra-fast agent execution loop supporting all VLM providers."""

    def __init__(self, ctx: ArtemisContext, goal: str, max_turns: int | None = None):
        self.ctx = ctx
        self.goal = goal
        try:
            cfg = load_agent_config()
            self.max_turns = max_turns if max_turns is not None else cfg.flash.max_turns
            self.max_silent_turns = getattr(cfg.flash, "max_silent_turns", 5)
            self.fast_burst = getattr(cfg.flash, "fast_burst", False)
            self.step_summarizer_cfg = cfg.flash.step_summarizer
            self.model_image_cfg = getattr(cfg.flash, "model_image", FlashModelImageConfig())
            self.compact_list = getattr(cfg.flash, "element_list", "full") == "compact"
            self.final_summary_wait_s = float(getattr(cfg.flash, "final_summary_wait_s", 3.0))
            self.media_resolution = _gemini_image_resolution(ctx)
            self.memory_runtime_cfg = cfg.memory.runtime
            self.transcript_cfg = cfg.memory.transcript
            self.chunking_cfg = cfg.memory.chunking
        except Exception:
            self.max_turns = max_turns if max_turns is not None else 0
            self.max_silent_turns = 5
            self.fast_burst = False
            self.step_summarizer_cfg = StepSummarizerConfig()
            self.model_image_cfg = FlashModelImageConfig()
            self.compact_list = False
            self.final_summary_wait_s = 3.0
            self.media_resolution = None
            self.memory_runtime_cfg = MemoryRuntimeConfig()
            self.transcript_cfg = MemoryTranscriptConfig()
            self.chunking_cfg = None

        # Observations made during this run (the runner's own and the executor's)
        # format the element list in the compact form; Pro never sets the flag.
        try:
            ctx.flash_compact_list = self.compact_list
        except (AttributeError, TypeError, ValueError):
            pass
        # 'on_demand': text-first turns, screenshot only when needed (see _send_image).
        self.image_on_demand = (
            getattr(getattr(self, "model_image_cfg", None), "send", "always") == "on_demand"
        )
        try:
            ctx.flash_relation_hints = self.image_on_demand and self.compact_list
        except (AttributeError, TypeError, ValueError):
            pass
        self.controller = UnifiedMobileController(ctx)
        # ``agent_name="flash"`` makes ask_explorer follow ``explorer.flash_mode``
        # (the Flash profile knob) instead of the Pro profile's tier.
        self.executor = McpActionExecutor(ctx, self.controller, agent_name="flash")
        self.summarizer = (
            VisualStepSummarizer(
                ctx,
                model_name=self.step_summarizer_cfg.model,
                retry_limit=self.memory_runtime_cfg.retry_limit,
                max_concurrency=self.memory_runtime_cfg.max_concurrency,
                # flush() only runs once the task has ended (final report, or the
                # context shutting down): summaries then feed no later turn, so
                # the user is not kept waiting on them for long.
                flush_timeout_s=min(self.memory_runtime_cfg.flush_timeout_s, self.final_summary_wait_s),
            )
            if self.step_summarizer_cfg.enabled
            else None
        )
        # Publish the service on the composition root (ctx.step_memory) so
        # any co-resident consumer shares this run's summary runtime.
        if self.summarizer is not None and getattr(ctx, "step_memory", None) is None:
            try:
                ctx.step_memory = self.summarizer
            except (AttributeError, TypeError, ValueError):
                pass

    @property
    def turn_limit(self) -> int | None:
        """The reactive turn cap, or ``None`` when the loop is unbounded."""
        try:
            limit = int(self.max_turns) if self.max_turns is not None else 0
        except (TypeError, ValueError):
            return None
        return limit if limit > 0 else None

    def _video_tools_enabled(self) -> bool:
        """Same gate the Pro graph applies before binding ``video_analyzer``."""
        setup = getattr(self.ctx, "execution_setup", None)
        return getattr(setup, "video_recording_tools_enabled", False) is True

    def _get_tools(self) -> list:
        # Flash has no tool that writes notes and its prompt never teaches
        # them, so the Validator set's note readers are left out here.
        tools = [
            t
            for t in VALIDATOR_TOOLS_DECLARATION
            if t is not READ_NOTE_TOOL and t is not LIST_NOTES_TOOL
        ]
        tools.insert(1, CLICK_SEQUENCE_TOOL)
        tools.append(WAIT_FOR_SCREEN_CHANGE_TOOL)
        tools.append(WAIT_FOR_TEXT_TOOL)
        tools.append(WAIT_UNTIL_GONE_TOOL)
        tools.append(ASK_EXPLORER_TOOL)
        # Helper tools shared with the Pro operator: the history tools are
        # declared from the very same args schemas the LangChain tools bind
        # (same availability gates: a DataEngine session, recall config).
        tools.extend(history_tool_declarations(self.ctx))
        if self._video_tools_enabled():
            from artemis.tools.video_tool import VIDEO_ANALYZER_TOOL

            tools.append(VIDEO_ANALYZER_TOOL)
        if getattr(self, "image_on_demand", False):
            tools.append(LOOK_AT_SCREEN_TOOL)
        tools.append(REPORT_TASK_STATUS_TOOL)
        # With an actuator installed, drop declarations for device actions the
        # backend does not implement (and append its extension tools); without
        # one, the full historical declaration set is kept.
        actuator = getattr(self.ctx, "actuator", None)
        if actuator is not None:
            from artemis.mcp.action_manifest import filter_declarations

            tools = filter_declarations(tools, actuator, "flash")
        return tools

    def _prune_intermediate_screenshots(self, messages: list[BaseMessage]) -> None:
        """Prunes binary screenshot blocks from intermediate observation messages."""
        prune_intermediate_screenshots(messages)

    # ------------------------------------------------------------------
    # run() setup helpers
    # ------------------------------------------------------------------

    def _build_ledger(self) -> TranscriptLedger:
        """Builds the session transcript ledger with the shared Pro history policy.

        The ``T+mm:ss`` clock is anchored to the DataEngine session start so
        the observation headers, the chunk ledger lines and the video
        analyzer's action timeline share one origin. L2/L3 chunk compression
        needs DataEngine step records; without an engine the ledger runs
        scrub-edge-only.
        """
        engine = getattr(self.ctx, "data_engine", None)
        session_start = getattr(engine, "session_start_time", None) if engine else None
        cfg = self.transcript_cfg
        ledger = TranscriptLedger(
            step_memory=self.summarizer,
            prune_history_xml=self.step_summarizer_cfg.prune_history_xml,
            image_scrub_depth=getattr(cfg, "image_scrub_depth", 3),
            pending_grace_steps=getattr(cfg, "pending_grace_steps", 3),
            xml_scrub_depth=getattr(cfg, "xml_scrub_depth", 1),
            image_scrub_depth_relaxed=getattr(cfg, "image_scrub_depth_relaxed", None),
            context_budget_tokens=getattr(cfg, "context_budget_tokens", None),
            start_ratio=getattr(cfg, "start_ratio", None),
            session_start=session_start if isinstance(session_start, (int, float)) else None,
        )
        if engine is not None:
            try:
                from artemis.memory import HistoryChunkManager

                ledger.attach_chunker(
                    HistoryChunkManager(
                        engine=engine,
                        ctx=self.ctx,
                        chunking_config=self.chunking_cfg,
                        transcript_config=cfg,
                        goal=self.goal,
                    )
                )
            except Exception as e:
                logger.error(f"History chunk manager unavailable for FlashRunner: {e}")
        try:
            self.ctx.transcript_ledger = ledger
        except (AttributeError, TypeError, ValueError):
            pass
        return ledger

    #: The model of the current run; decides how screenshot tool results travel.
    _llm = None

    def _init_llm(self):
        """Initializes the Universal LLM via the Service Layer."""
        try:
            return get_llm(self.ctx, name="operator")
        except Exception as e:
            logger.warning(f"Failed to get operator LLM from config, using default: {e}")

            return RobustChatModelWrapper(get_google_llm(model_name="gemini-2.5-flash"), self.ctx)

    def _render_system_prompt(self, tools_declaration: list) -> str:
        """Renders the system prompt from the flash_runner.md template.

        Tool-teaching segments are gated on the available tool set so an absent
        tool leaves no trace in the prompt; until an actuator is wired in, the
        full manifest set reproduces the historical prompt.
        """
        prompt_path = Path(__file__).parent / "flash_runner.md"
        prompt_template = prompt_path.read_text(encoding="utf-8")
        available_tools = frozenset(t.name for t in tools_declaration)
        return Template(prompt_template).render(
            goal=self.goal,
            available_tools=available_tools,
            compact_list=getattr(self, "compact_list", False),
        )

    # ------------------------------------------------------------------
    # Per-turn helpers (observe / think)
    # ------------------------------------------------------------------

    async def _read_injected_instruction(
        self, offset_label: str | None = None
    ) -> tuple[str | None, UserGuidance | None]:
        """Consumes this turn's injected instruction: ``(instruction, guidance)``.

        ``instruction`` is the user's text verbatim — what the step record,
        the chunk ledger and the visual-transition lens receive (the same
        shape Pro stamps). ``guidance`` is the shared user-guidance pair
        (:func:`render_user_guidance`, the Pro rendering without a plan to
        edit): the ephemeral ``--- User Guidance ---`` wrapper and the
        persistent verbatim body, both riding on the observation tail.
        ``offset_label`` stamps the body with the session offset. Both are
        None when nothing was injected.
        """
        if not (self.ctx.data_engine and self.ctx.data_engine.base_dir):
            return None, None
        try:
            injected_payload = await asyncio.to_thread(
                _check_injected_instruction_file,
                str(self.ctx.data_engine.base_dir),
            )
        except (OSError, ValueError, AttributeError) as e:
            logger.warning(f"Failed to check injected instruction in FlashRunner: {e}")
            return None, None
        if not (injected_payload and injected_payload.get("instruction")):
            return None, None
        instruction = str(injected_payload["instruction"])
        guidance = render_user_guidance(
            instruction,
            release_loop=bool(injected_payload.get("release_loop")),
            has_plan=False,
            offset_label=offset_label,
        )
        return instruction, guidance

    def _build_tail(
        self,
        ledger: TranscriptLedger,
        turns: int,
        img_bytes,
        xml_list,
        *,
        injected: UserGuidance | None = None,
        notices: list[str] | None = None,
        is_final: bool = False,
        previous_turn_silent: bool = False,
        scrollable: bool | None = None,
    ) -> HumanMessage:
        """Builds this turn's observation tail (Pro observation shape).

        Header, screenshot and UI list carry the same markers as the Pro
        operator's tail so the ledger's scrub edge treats both alike. The
        objective is stated once, in the system prompt; the tail never repeats
        it. Every per-turn notice (nudges, the user-guidance wrapper, the
        final-turn warning, the reasoning reminder after a silent turn) is
        marked ephemeral so it leaves the transcript with the recent window.
        The verbatim body of an injected instruction is the one exception: it
        is a regular block, so a standing instruction stays in the active
        window until the turn is chunked (the chunk's user lines carry it on).
        """
        # ``turns`` is kept for call-site symmetry: the tail has the same shape
        # on every turn.
        obs_header = f"# CURRENT OBSERVATION [{ledger.elapsed_label()}]"
        if scrollable is True:
            obs_header += "\n[Scroll: scrollable container detected]"
        elif scrollable is False:
            has_xml = bool(xml_list and xml_list.strip() and "Not available" not in xml_list)
            if has_xml:
                obs_header += "\n[Scroll: fixed layout (no scrollable container — scrolling is unlikely to reveal anything)]"
            else:
                obs_header += "\n[Scroll: unknown]"

        blocks: list[dict] = [
            {"type": "text", "text": obs_header}
        ]
        if img_bytes and not self._send_image(turns, img_bytes, xml_list, bool(notices)):
            img_bytes = None
            blocks.append({"type": "text", "text": TEXT_ONLY_NOTE})
        if img_bytes:
            # Only the model's copy shrinks; OCR, guards and traces keep the original.
            image_cfg = getattr(self, "model_image_cfg", None)
            if image_cfg is not None and getattr(image_cfg, "max_side", 0):
                img_bytes = shrink_for_model(img_bytes, image_cfg.max_side, image_cfg.jpeg_quality)
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            blocks.append({"type": "text", "text": "--- Current Screenshot ---"})
            blocks.append(self._image_block(img_b64))
        if xml_list:
            blocks.append({"type": "text", "text": f"{PRO_UI_LIST_MARKER}\n{xml_list}"})
        ephemeral: list[int] = []
        # (text, ephemeral) in tail order.
        per_turn: list[tuple[str, bool]] = [(text, True) for text in (notices or [])]
        if injected:
            # The wrapper is per-turn; the verbatim body right after it is a
            # regular block that stays until the turn is chunked.
            per_turn.append((injected.wrapper, True))
            per_turn.append((injected.body, False))
        if is_final:
            per_turn.append((_FINAL_TURN_WARNING, True))
        if previous_turn_silent:
            per_turn.append((REASONING_REMINDER, True))
        for text, is_ephemeral in per_turn:
            if is_ephemeral:
                ephemeral.append(len(blocks))
            blocks.append({"type": "text", "text": text})
        tail = HumanMessage(content=blocks)
        if ephemeral:
            mark_ephemeral(tail, ephemeral)
        return tail

    def _send_image(self, turn: int, img_bytes: bytes, xml_list: str | None, has_notices: bool) -> bool:
        """'on_demand' mode: whether this turn's tail carries the screenshot.

        Always for 'always'. Otherwise the screenshot goes on the first turn, when
        asked for (look_at_screen, a deferred completion), after a notice (failed or
        no-effect action, timeout handover), when the pixels changed although the
        element list did not (an error highlight, toast or spinner only the image
        shows), and after ``text_max_consecutive`` text-only turns. A re-asked turn
        keeps its first decision.
        """
        if not getattr(self, "image_on_demand", False):
            self._text_only_turn = False
            return True
        decided = getattr(self, "_image_decision", None)
        if decided and decided[0] == turn:
            return decided[1]
        cfg = getattr(self, "model_image_cfg", None)
        sig = _list_signature(xml_list)
        prev_sig = getattr(self, "_prev_list_sig", None)
        prev_bytes = getattr(self, "_prev_obs_bytes", None)
        text_turns = getattr(self, "_text_turns", 0)
        reason = ""
        if prev_bytes is None:
            reason = "first"
        elif getattr(self, "_force_image", False):
            reason = "requested"
        elif has_notices:
            reason = "notice"
        elif text_turns >= getattr(cfg, "text_max_consecutive", 4):
            reason = "cap"
        elif prev_sig is not None and _jaccard(sig, prev_sig) >= 0.9 and _pixels_changed(prev_bytes, img_bytes):
            reason = "pixel-only change"
        send = bool(reason)
        self._force_image = False
        self._text_turns = 0 if send else text_turns + 1
        self._prev_list_sig = sig
        self._prev_obs_bytes = img_bytes
        self._text_only_turn = not send
        self._image_decision = (turn, send)
        logger.info(f"Flash on_demand: turn {turn} {'image (' + reason + ')' if send else 'text only'}")
        return send

    def _extract_response_text(self, response) -> str:
        """Extracts the natural-language thought text from the model response."""
        raw_text = response.content if isinstance(response.content, str) else ""
        if isinstance(response.content, list):
            raw_text = "".join(
                b.get("text", "") for b in response.content if isinstance(b, dict) and "text" in b
            )
        return raw_text

    def _extract_response_thinking(self, response) -> str:
        """Extracts the model's native thinking blocks (thought summaries), if any.

        Providers running with ``include_thoughts`` return them as ``thinking``
        content blocks next to the ``text`` blocks; they are recorded on the step
        as ``operator_native_thinking`` and never fed back as reply text.
        """
        if not isinstance(response.content, list):
            return ""
        parts = [
            str(b.get("thinking") or "")
            for b in response.content
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        return "\n".join(p for p in parts if p.strip())

    def _token_usage_from_response(self, response) -> dict | None:
        """Extracts token usage metadata from the model response, if present."""
        step_token_usage = None
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            u = response.usage_metadata
            pr = u.get("input_tokens") or u.get("prompt_tokens") or 0
            co = u.get("output_tokens") or u.get("completion_tokens") or 0
            to = u.get("total_tokens") or (pr + co)
            if to > 0:
                step_token_usage = {
                    "prompt_tokens": int(pr),
                    "completion_tokens": int(co),
                    "total_tokens": int(to),
                }
        elif hasattr(response, "response_metadata") and isinstance(
            response.response_metadata, dict
        ):
            u = response.response_metadata.get("usage_metadata") or response.response_metadata.get(
                "token_usage"
            )
            if isinstance(u, dict):
                pr = (
                    u.get("input_tokens")
                    or u.get("prompt_tokens")
                    or u.get("prompt_token_count")
                    or 0
                )
                co = (
                    u.get("output_tokens")
                    or u.get("completion_tokens")
                    or u.get("candidates_token_count")
                    or 0
                )
                to = u.get("total_tokens") or u.get("total_token_count") or (pr + co)
                if to > 0:
                    step_token_usage = {
                        "prompt_tokens": int(pr),
                        "completion_tokens": int(co),
                        "total_tokens": int(to),
                    }
        return step_token_usage

    def _image_block(self, img_b64: str) -> dict:
        """The screenshot content block for the model.

        With a Gemini ``media_resolution`` configured on the operator node, the
        image goes out as a standard ``image`` block carrying a per-part
        resolution (langchain-google-genai ignores the model-level setting on
        Gemini 3; per part, 'low' cuts ~1080 image tokens to ~270). Otherwise the
        historical ``image_url`` data URL, which every provider understands.
        """
        level = getattr(self, "media_resolution", None)
        if level:
            return {
                "type": "image",
                "base64": img_b64,
                "mime_type": "image/jpeg",
                "media_resolution": f"MEDIA_RESOLUTION_{level.upper()}",
            }
        return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}

    def _estimate_token_usage(self, messages: list[BaseMessage], raw_text: str) -> dict:
        """Approximates token usage from message sizes when metadata is absent."""
        calc_prompt_chars = 0
        calc_images = 0
        for msg in messages:
            c = getattr(msg, "content", "")
            if isinstance(c, str):
                calc_prompt_chars += len(c)
            elif isinstance(c, list):
                for block in c:
                    if isinstance(block, dict):
                        if block.get("type") in ("image_url", "image") or "image_url" in block:
                            calc_images += 1
                        else:
                            calc_prompt_chars += len(str(block.get("text", "")))
                    else:
                        calc_prompt_chars += len(str(block))
        prompt_tokens = (calc_prompt_chars // 4) + (calc_images * 258)
        completion_tokens = len(raw_text) // 4
        return {
            "prompt_tokens": max(1, prompt_tokens),
            "completion_tokens": max(1, completion_tokens),
            "total_tokens": max(1, prompt_tokens + completion_tokens),
        }

    def _resolve_token_usage(
        self, response, messages: list[BaseMessage], raw_text: str
    ) -> tuple[dict, bool]:
        """Token usage for the call and whether the provider measured it.

        Falls back to an estimate when the response carries no usage
        metadata; the flag lets the caller keep an estimate out of the
        ledger's chars-per-token calibration (an estimate is itself derived
        from character counts, so calibrating on it would be circular).
        """
        step_token_usage = self._token_usage_from_response(response)
        if step_token_usage and step_token_usage.get("total_tokens", 0) > 0:
            return step_token_usage, True
        return self._estimate_token_usage(messages, raw_text), False

    def _record_llm_trace(self, step_token_usage: dict, raw_text: str) -> None:
        """Records the llm_call trace for this turn in the DataEngine."""
        if self.ctx.data_engine:
            current_step_id = getattr(self.ctx.data_engine, "current_step_id", None)
            self.ctx.data_engine.record_trace(
                type="llm_call",
                name="FlashRunner",
                payload={"token_usage": step_token_usage, "response": raw_text},
                step_id=current_step_id,
                status="success",
            )

    def _resolve_tool_calls(self, response, raw_text: str) -> list:
        """Returns native tool calls, falling back to text parsing if absent."""
        tool_calls = response.tool_calls or []

        # Fallback text parsing if tool_calls not parsed natively
        if not tool_calls and "```json" in raw_text:
            parsed = parse_structured(raw_text)
            if isinstance(parsed, ParseFailure):
                logger.warning(
                    "FlashRunner response contained a JSON block that could"
                    f" not be parsed: {parsed.error}"
                )
            elif isinstance(parsed, dict) and "name" in parsed:
                tool_calls = [
                    {
                        "name": parsed["name"],
                        "args": parsed.get("args", {}),
                        "id": str(uuid.uuid4()),
                    }
                ]
        return tool_calls

    # ------------------------------------------------------------------
    # Per-tool-call helpers (act / record / report)
    # ------------------------------------------------------------------

    async def _finalize_task_report(
        self,
        name: str,
        args: dict,
        tc_id: str,
        raw_text: str,
        step_token_usage: dict,
        pre_screenshot_bytes,
        xml_list,
        messages: list[BaseMessage],
        native_text: str | None = None,
    ) -> dict:
        """Records the final step, acknowledges the tool call, and flushes."""
        final_report = args
        if self.ctx.data_engine:
            try:
                if self.ctx.data_engine.current_step_id is None:
                    self.ctx.data_engine.allocate_step_id()
                self.ctx.data_engine.record_step(
                    pre_screenshot_bytes=pre_screenshot_bytes,
                    ui_tree=xml_list,
                    action_taken={"action": "report_task_status", "args": args},
                    operator_raw_thinking=raw_text,
                    operator_native_thinking=native_text or None,
                    last_execution_result={"result": "Task completed with final report."},
                    extra_metadata={"token_usage": step_token_usage},
                )
            except Exception as step_err:
                logger.warning(f"Error recording final step in FlashRunner: {step_err}")
        messages.append(
            ToolMessage(
                tool_call_id=tc_id,
                name=name,
                content=json.dumps({"status": "acknowledged"}),
                status="success",
            )
        )
        await self._flush_history_jobs()
        return final_report

    async def _flush_history_jobs(self) -> None:
        """Ends the run's background summaries within ``final_summary_wait_s``.

        Step summaries and chunk capsules only compress history for later
        turns; once the task is over they feed nothing, so the user is not
        kept waiting on them (the context's own shutdown flush then finds
        nothing left to drain).
        """
        if self.summarizer:
            await self.summarizer.flush()
        ledger = getattr(self.ctx, "transcript_ledger", None)
        chunker = getattr(ledger, "chunker", None)
        if chunker is not None and callable(getattr(chunker, "flush", None)):
            try:
                await chunker.flush(timeout_seconds=getattr(self, "final_summary_wait_s", 3.0))
            except Exception as e:
                logger.debug(f"History chunk flush failed; skipped: {e}")

    def _rebase_screen_baseline(self, obs, screenshot_bytes) -> None:
        """Tells the executor about a screen captured outside it, so its
        screen-unchanged check (repeat-tap guard, post-tap recapture) compares
        against the screen the model will actually see next."""
        record = getattr(self.executor, "record_observation", None)
        if not callable(record):
            return
        try:
            record(obs.elements if obs.hierarchy_ok else None, screenshot_bytes)
        except Exception as err:
            logger.debug(f"Could not re-base the executor's screen baseline: {err}")

    def _raise_if_device_offline(self, *, obs: Any = None, exec_result: Any = None) -> None:
        """Fail fast when observation or execution reports that the device disconnected."""
        device_id = getattr(getattr(self.ctx, "device", None), "device_id", None) or "unknown"
        if obs is not None and is_device_offline_result(obs):
            raise DeviceOfflineError(device_id, None, message=getattr(obs, "message", None))
        meta = getattr(exec_result, "metadata", None)
        if isinstance(meta, dict) and meta.get("device_offline") is True:
            raise DeviceOfflineError(
                device_id, None, message=getattr(exec_result, "text_summary", None)
            )

    async def _capture_post_screenshot(
        self, exec_result, name: str, action_names, state: State | None = None
    ):
        """Returns the post-action screenshot, capturing a fallback if needed.

        When a device action produced no observation (a failed action, an
        unknown tool, ...) the screen is observed through the very same
        pipeline the executor's success path uses (:func:`observe`), so the
        next observation tail carries the indexed minimal UI list and
        ``state`` gets the same refreshed screenshot path and indexed
        elements. Nothing is captured for non-action tools.
        """
        post_img_bytes = exec_result.screenshot_bytes
        if post_img_bytes or name not in action_names:
            return post_img_bytes
        try:
            obs, screenshot_bytes = await observe(self.ctx, self.controller, settle_ms=0)
        except DeviceOfflineError:
            raise
        except Exception as shot_err:
            logger.warning(f"Failed to capture fallback screenshot in FlashRunner: {shot_err}")
            return post_img_bytes
        self._raise_if_device_offline(obs=obs)
        if not obs.ok:
            logger.warning(f"Failed to capture fallback screenshot in FlashRunner: {obs.message}")
            return post_img_bytes
        post_img_bytes = screenshot_bytes
        self._rebase_screen_baseline(obs, screenshot_bytes)
        if not exec_result.ui_elements_text:
            exec_result.ui_elements_text = obs.elements_text
        if not exec_result.screenshot_path and obs.screenshot_path:
            exec_result.screenshot_path = obs.screenshot_path
        if state is not None:
            if obs.hierarchy_ok:
                state.indexed_points = [el["center"] for el in obs.elements]
                state.indexed_elements = obs.elements
            if obs.screenshot_path:
                state.latest_screenshot = obs.screenshot_path
        return post_img_bytes

    def _extract_normalized_coordinates(self, name: str, args: dict, resolved=None):
        """Extracts and enriches coordinate metadata for the recorded action.

        ``resolved`` is the normalized point the executor sent a point action
        to; it is the coordinate of record when the model addressed an element
        by index (the arguments then carry no point at all).
        """
        norm_coords = None
        norm_start = None
        norm_end = None
        if name in ("click", "tap", "long_press", "input_text") and (
            isinstance(resolved, (list, tuple)) and len(resolved) == 2
        ):
            return [int(resolved[0]), int(resolved[1])], None, None
        if name == "swipe":
            kind, target_val, _ = parse_swipe_parameters(args)
            if kind == "coords" and isinstance(target_val, list) and len(target_val) == 4:
                norm_coords = target_val
                norm_start = target_val[:2]
                norm_end = target_val[2:]
            elif kind == "direction" and isinstance(target_val, str):
                g_lower = target_val.lower()
                if "up" in g_lower:
                    norm_coords = [600, 700, 600, 300]
                elif "down" in g_lower:
                    norm_coords = [600, 300, 600, 700]
                elif "left" in g_lower:
                    norm_coords = [750, 500, 250, 500]
                elif "right" in g_lower:
                    norm_coords = [250, 500, 750, 500]
                if norm_coords:
                    norm_start = norm_coords[:2]
                    norm_end = norm_coords[2:]
        elif name in ("click", "tap", "long_press", "input_text"):
            target = args.get("target") or args.get("coordinates")
            if isinstance(target, (list, tuple)) and len(target) == 2:
                norm_coords = list(target)
            elif isinstance(target, str):
                nums = re.findall(r"-?\d+(?:\.\d+)?", target)
                if len(nums) == 2:
                    norm_coords = [int(float(nums[0])), int(float(nums[1]))]
        return norm_coords, norm_start, norm_end

    def _build_action_record(self, name: str, args: dict, exec_result) -> dict:
        """The action as the step record and the visual lens see it.

        Flash records the model's own 0–1000 coordinates verbatim (an index
        target records the point the executor resolved it to); the explicit
        space marker keeps every later normalization pass (agent-friendly
        steps, MCP inspector) a no-op on this record. The tool arguments ride
        along under ``args``, and the target semantics the executor validated
        (observed element fields for an index, the model's own
        ``target_description`` for a point) land top level in the shape Pro
        records.
        """
        metadata = getattr(exec_result, "metadata", None) or {}
        resolved = metadata.get("target_coordinates")
        norm_coords, norm_start, norm_end = self._extract_normalized_coordinates(
            name, args, resolved=resolved
        )
        given = args.get("target") or args.get("coordinates") or args.get("sequence")
        if isinstance(given, (int, float)) and not isinstance(given, bool):
            given = None  # An element index is not a coordinate.
        action_dict = {
            "action": name,
            "coordinates": given or norm_coords,
            COORDINATE_SPACE_KEY: COORDINATE_SPACE_NORMALIZED,
            "args": args,
        }
        if norm_coords:
            action_dict["normalized_coordinates"] = norm_coords
        if norm_start and norm_end:
            action_dict["normalized_start_coordinates"] = norm_start
            action_dict["normalized_end_coordinates"] = norm_end
        action_dict.update(metadata.get("target_semantics") or {})
        return action_dict

    def _record_action_step(
        self,
        name: str,
        args: dict,
        exec_result,
        raw_text: str,
        step_token_usage: dict,
        pre_screenshot_bytes,
        xml_list,
        post_img_bytes,
        injected: str | None = None,
        native_text: str | None = None,
        action_dict: dict | None = None,
    ):
        """Records telemetry / step in DataEngine; returns the step id or None."""
        recorded_step_id = None
        try:
            if self.ctx.data_engine.current_step_id is None:
                self.ctx.data_engine.allocate_step_id()

            if action_dict is None:
                action_dict = self._build_action_record(name, args, exec_result)

            succeeded = exec_result.status == "success"
            last_execution_result = {
                "status": "dispatched" if succeeded else "failed",
                "result": exec_result.text_summary,
            }
            if not succeeded:
                last_execution_result["error"] = exec_result.text_summary

            extra_metadata: dict = {"token_usage": step_token_usage}
            timing = self._step_timing(exec_result)
            if timing:
                extra_metadata["timing"] = timing
            if injected:
                # Stamped verbatim on the step it reached: the chunk ledger
                # keeps it as a never-evicted line at every compression level.
                extra_metadata["injected_instruction"] = injected

            recorded_step_id = self.ctx.data_engine.record_step(
                pre_screenshot_bytes=pre_screenshot_bytes,
                post_screenshot_bytes=post_img_bytes,
                ui_tree=(exec_result.ui_elements_text or xml_list),
                action_taken=action_dict,
                operator_raw_thinking=raw_text,
                operator_native_thinking=native_text or None,
                last_execution_result=last_execution_result,
                extra_metadata=extra_metadata,
            )
            self._notify_history_chunker(recorded_step_id)
        except Exception as step_err:
            logger.warning(f"Error recording step in FlashRunner: {step_err}")
        return recorded_step_id

    def _notify_history_chunker(self, step_id) -> None:
        """Stamps the recorded step for the chunk manager (single segment, no plan)."""
        ledger = getattr(self.ctx, "transcript_ledger", None)
        chunker = getattr(ledger, "chunker", None) if ledger is not None else None
        if chunker is None or step_id is None:
            return
        try:
            chunker.on_step_stamped(str(step_id), None)
        except Exception as e:
            logger.warning(f"History chunker step stamp failed: {e}")

    async def _execute_and_record_action(
        self,
        name: str,
        args: dict,
        tc_id: str,
        state: State,
        messages: list[BaseMessage],
        raw_text: str,
        step_token_usage: dict,
        pre_screenshot_bytes,
        xml_list,
        action_sequence: int,
        turn: _TurnRecord,
        injected: str | None = None,
        native_text: str | None = None,
        index_elements: list[dict] | None = None,
        skip_observe: bool = False,
        verify_live_target: bool = False,
    ):
        """Executes one tool call, records it, and updates loop state.

        The tool message carries the outcome text only; the post-action
        screenshot and UI list become the next turn's observation tail.
        ``index_elements`` is the turn's element snapshot index targets
        resolve against (see :meth:`_process_tool_calls`); ``None`` resolves
        against the state's current list. Returns the (possibly updated)
        pre_screenshot_bytes, xml_list, and action_sequence for the next
        iteration.
        """
        # Dynamic dispatch set: manifest device actions plus any backend
        # extension tools, so extension steps are recorded like actions.
        action_names = self.executor.action_tool_names
        try:
            exec_result = await self.executor.execute(
                name, args, tc_id, state, index_elements=index_elements, skip_observe=skip_observe,
                verify_live_target=verify_live_target,
            )
            self._raise_if_device_offline(exec_result=exec_result)

            post_img_bytes = None
            if not skip_observe:
                post_img_bytes = await self._capture_post_screenshot(
                    exec_result, name, action_names, state
                )

            # One record shape for the DataEngine step and the visual lens.
            action_dict = (
                self._build_action_record(name, args, exec_result) if name in action_names else None
            )

            # Record telemetry / step in DataEngine
            recorded_step_id = None
            if self.ctx.data_engine and name in action_names:
                recorded_step_id = self._record_action_step(
                    name,
                    args,
                    exec_result,
                    raw_text,
                    step_token_usage,
                    pre_screenshot_bytes,
                    xml_list,
                    post_img_bytes,
                    injected=injected,
                    native_text=native_text,
                    action_dict=action_dict,
                )

            rec = getattr(self, "_turn_rec", None)
            if rec is not None:
                rec["actions"].append(turn_log.action_entry(name, args, exec_result))

            if name in action_names:
                turn.step_keys.append(str(recorded_step_id) if recorded_step_id else str(tc_id))
                turn.actions.append((name, exec_result.status, exec_result.text_summary))

            # ⚡ Non-blocking dispatch of objective visual transition summarizer
            if self.summarizer and name in action_names:
                action_sequence += 1
                self.summarizer.dispatch(
                    step_number=action_sequence,
                    action_name=name,
                    # The recorded action minus its verb: the same shape the Pro
                    # summarizer hands the lens, so the phrase renders alike.
                    action_args={k: v for k, v in action_dict.items() if k != "action"},
                    pre_img_bytes=pre_screenshot_bytes,
                    post_img_bytes=post_img_bytes,
                    exec_outcome=exec_result.text_summary,
                    action_key=str(tc_id),
                    data_engine_step_id=recorded_step_id,
                    # The model's own reasoning for this turn steers the lens's
                    # attention; the target it named rides on ``args``.
                    focus=build_focus_context(
                        intent=raw_text,
                        goal=self.goal,
                        injected_instruction=injected,
                    ),
                )

            if post_img_bytes:
                pre_screenshot_bytes = post_img_bytes
            if exec_result.ui_elements_text:
                xml_list = exec_result.ui_elements_text

            # Helper tools may return multimodal blocks (a step screenshot);
            # device actions always report text — their post screenshot is the
            # next observation tail, never a tool-message image. The image
            # carrier follows the model's provider (tool_result_messages).
            raw_blocks = exec_result.raw_result if name not in action_names else None
            content = (
                raw_blocks
                if isinstance(raw_blocks, list) and raw_blocks
                else exec_result.text_summary or f"Action '{name}' dispatched."
            )
            messages.extend(
                tool_result_messages(
                    tc_id, content, name=name, status=exec_result.status, llm=self._llm
                )
            )

        except DeviceOfflineError:
            raise
        except Exception as e:
            logger.error(f"Error executing tool {name}: {e}")
            rec = getattr(self, "_turn_rec", None)
            if rec is not None:
                rec["actions"].append(
                    {"tool": name, "args": turn_log.compact_args(args), "status": "error", "msg": str(e)[:160]}
                )
            if name in action_names:
                turn.step_keys.append(str(tc_id))
                turn.actions.append((name, "error", f"Error executing tool {name}: {e}"))
            messages.append(
                ToolMessage(
                    tool_call_id=tc_id,
                    name=name,
                    content=f"Error executing tool {name}: {e}",
                    status="error",
                )
            )
        return pre_screenshot_bytes, xml_list, action_sequence

    async def _invoke_model(self, llm, current_tools: list, messages: list[BaseMessage]):
        """Binds the active tools and invokes the model through the LLM gateway."""
        # Bind active tools
        bound_llm = llm.bind_tools(current_tools)

        # Invoke Model. Streaming, live-token UI deltas, classified
        # retries, and pause/resume are all owned by the LLM gateway
        # (acomplete); a typed LLMCallError propagates if it gives up.
        # A resumed pause raises LLMResumedError instead of replaying this
        # request: the run loop re-observes the device and asks again.
        with reobserve_on_resume():
            return await invoke_llm_with_timeout_message(
                acomplete(bound_llm, messages), timeout_seconds=10, hard_timeout=180
            )

    async def _process_tool_calls(
        self,
        tool_calls: list,
        state: State,
        messages: list[BaseMessage],
        raw_text: str,
        step_token_usage: dict,
        pre_screenshot_bytes,
        xml_list,
        action_sequence: int,
        turn: _TurnRecord,
        injected: str | None = None,
        native_text: str | None = None,
    ):
        """Dispatches the turn's tool calls.

        Every index target of the turn resolves against the element list the
        model decided on: the list of the observation this turn was built
        from, snapshotted here before the first action. Each action still
        refreshes ``state.indexed_elements`` (that refreshed list is what the
        next observation shows), so without the snapshot ``click(5)`` after
        ``click(3)`` would be resolved against the screen after the first
        click.

        The snapshot is the very list object the observation left on the
        state (not a copy), and observations replace the state's list rather
        than mutating it, so ``ask_explorer`` candidates registered before
        any action land in the snapshot by construction. When a helper tool
        appends candidates after an action has already replaced the state's
        list, the explorer numbered them against that replaced list, so the
        snapshot is re-taken from it: indices the explorer just handed out
        resolve correctly, at the cost of the pre-action numbering (mixing
        the two in one turn is the model's error, not a supported case).

        Returns (final_report, pre_screenshot_bytes, xml_list, action_sequence);
        final_report is non-None only when 'report_task_status' was called.
        """
        action_names = self.executor.action_tool_names
        index_elements = self._turn_index_snapshot(state)
        total_calls = len(tool_calls)
        has_waited = False
        # Device actions dispatched so far in this burst (point actions after the
        # first are re-checked against the live screen by the executor).
        actions_executed_in_burst = 0

        for idx, tc in enumerate(tool_calls):
            name = tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"]
            args = tc.get("args") or {}
            tc_id = tc.get("id") or str(uuid.uuid4())
            is_last = (idx == total_calls - 1)
            is_wait_tool = name in ("wait_for_text", "wait_for_screen_change", "wait_until_gone")

            # Safety check: After a wait step in a burst, actions with stale coordinates/indices are aborted.
            # Actions without targets (press_key, input_text without target, directional swipe, manage_app)
            # or actions using target_text remain valid.
            if has_waited and (name in action_names) and _has_stale_screen_target(name, args):
                logger.warning(
                    f"Action '{name}' after wait step uses stale target coordinates or index. "
                    f"Indices or coordinates from previous screen are invalid after waiting. "
                    f"Aborting burst to observe new screen."
                )
                try:
                    obs, shot_bytes = await observe(self.ctx, self.controller, settle_ms=0)
                    self._raise_if_device_offline(obs=obs)
                    if obs.ok:
                        pre_screenshot_bytes = shot_bytes
                        self._rebase_screen_baseline(obs, shot_bytes)
                        if obs.elements_text:
                            xml_list = obs.elements_text
                        if state is not None:
                            if obs.hierarchy_ok:
                                state.indexed_points = [el["center"] for el in obs.elements]
                                state.indexed_elements = obs.elements
                            if obs.screenshot_path:
                                state.latest_screenshot = obs.screenshot_path
                except DeviceOfflineError:
                    raise
                except Exception as shot_err:
                    logger.warning(f"Failed to observe screen after aborting burst: {shot_err}")

                safety_msg = (
                    f"Safety guard: Action '{name}' follows a wait step or cross-screen action and targets "
                    f"an index or coordinate from an earlier screen. Burst aborted to observe the new screen state."
                )
                if name in action_names:
                    turn.step_keys.append(str(tc_id))
                    turn.actions.append((name, "error", safety_msg))
                messages.append(
                    ToolMessage(
                        tool_call_id=tc_id,
                        name=name,
                        content=safety_msg,
                        status="error",
                    )
                )
                for rem_tc in tool_calls[idx + 1:]:
                    rem_id = rem_tc.get("id") or str(uuid.uuid4())
                    rem_name = rem_tc.get("name", "").split(":")[-1]
                    messages.append(
                        ToolMessage(
                            tool_call_id=rem_id,
                            name=rem_name,
                            content="Action skipped: Burst was aborted due to safety guard.",
                            status="error",
                        )
                    )
                break

            # Safety check: Commitment action cannot run in the middle of a burst
            target_desc = str(args.get("target_description") or args.get("text") or "")
            target_val = args.get("target")
            if not target_desc and index_elements:
                try:
                    t_idx = int(target_val) if isinstance(target_val, (int, str)) and str(target_val).isdigit() else None
                    if t_idx is not None:
                        # Visible UI elements are 1-indexed (index_elements[t_idx - 1] has index == t_idx)
                        matching_el = None
                        if 1 <= t_idx <= len(index_elements):
                            candidate = index_elements[t_idx - 1]
                            if candidate.get("index") == t_idx:
                                matching_el = candidate
                        if matching_el is None:
                            matching_el = next((el for el in index_elements if el.get("index") == t_idx), None)
                        if matching_el:
                            target_desc = str(matching_el.get("text") or matching_el.get("content_description") or "")
                except Exception:
                    pass

            is_commitment = bool(COMMITMENT_PATTERN.search(target_desc))
            if is_commitment and not is_last:
                logger.warning(
                    f"Commitment action '{name}' ('{target_desc}') detected mid-burst (call {idx+1}/{total_calls}). "
                    f"Aborting burst before executing commitment action for user safety."
                )
                try:
                    obs, shot_bytes = await observe(self.ctx, self.controller, settle_ms=0)
                    self._raise_if_device_offline(obs=obs)
                    if obs.ok:
                        pre_screenshot_bytes = shot_bytes
                        self._rebase_screen_baseline(obs, shot_bytes)
                        if obs.elements_text:
                            xml_list = obs.elements_text
                        if state is not None:
                            if obs.hierarchy_ok:
                                state.indexed_points = [el["center"] for el in obs.elements]
                                state.indexed_elements = obs.elements
                            if obs.screenshot_path:
                                state.latest_screenshot = obs.screenshot_path
                except DeviceOfflineError:
                    raise
                except Exception as shot_err:
                    logger.warning(f"Failed to observe screen after aborting burst: {shot_err}")

                safety_msg = (
                    f"Safety guard: Action '{name}' targeting commitment button '{target_desc}' cannot be "
                    f"executed in the middle of a fast burst. All remaining actions aborted. "
                    f"Please verify the screen state and execute commitment actions individually."
                )
                if name in action_names:
                    turn.step_keys.append(str(tc_id))
                    turn.actions.append((name, "error", safety_msg))
                messages.append(
                    ToolMessage(
                        tool_call_id=tc_id,
                        name=name,
                        content=safety_msg,
                        status="error",
                    )
                )
                for rem_tc in tool_calls[idx + 1:]:
                    rem_id = rem_tc.get("id") or str(uuid.uuid4())
                    rem_name = rem_tc.get("name", "").split(":")[-1]
                    messages.append(
                        ToolMessage(
                            tool_call_id=rem_id,
                            name=rem_name,
                            content="Action skipped: Burst was aborted due to safety guard.",
                            status="error",
                        )
                    )
                break

            is_wait_tool = name in ("wait_for_text", "wait_for_screen_change", "wait_until_gone")
            skip_observe = self.fast_burst and (not is_last) and (name in action_names) and (not is_wait_tool)
            logger.info(f"Executing Flash tool: {name}({args}) [skip_observe={skip_observe}]")

            defer_completion = (
                name == "report_task_status"
                and getattr(self, "image_on_demand", False)
                and getattr(self, "_text_only_turn", False)
                and str(args.get("status", "")).lower() == "completed"
            )
            if name == "look_at_screen" or defer_completion:
                # Text mode: the next observation carries the screenshot. A completion
                # report from a turn that saw no screenshot is checked on it first.
                self._force_image = True
                content = (
                    "The next observation includes the screenshot."
                    if name == "look_at_screen"
                    else "Completion not accepted yet: this turn had no screenshot. The next observation "
                    "includes it; verify the goal on it, then report again."
                )
                messages.append(
                    ToolMessage(
                        tool_call_id=tc_id,
                        name=name,
                        content=content,
                        status="success" if name == "look_at_screen" else "error",
                    )
                )
                for rem_tc in tool_calls[idx + 1:]:
                    messages.append(
                        ToolMessage(
                            tool_call_id=rem_tc.get("id") or str(uuid.uuid4()),
                            name=rem_tc.get("name", "").split(":")[-1],
                            content="Action skipped: the turn ended to show the screenshot.",
                            status="error",
                        )
                    )
                return None, pre_screenshot_bytes, xml_list, action_sequence

            if name == "report_task_status":
                probed = await self._probe_unseen_content(
                    args,
                    tc_id,
                    state,
                    messages,
                    raw_text,
                    step_token_usage,
                    pre_screenshot_bytes,
                    xml_list,
                    action_sequence,
                    turn,
                    native_text=native_text,
                )
                if probed is not None:
                    # More content was below: the report is deferred and the
                    # model continues from the scrolled screen.
                    pre_screenshot_bytes, xml_list, action_sequence = probed
                    return None, pre_screenshot_bytes, xml_list, action_sequence
                final_report = await self._finalize_task_report(
                    name,
                    args,
                    tc_id,
                    raw_text,
                    step_token_usage,
                    pre_screenshot_bytes,
                    xml_list,
                    messages,
                    native_text=native_text,
                )
                return final_report, pre_screenshot_bytes, xml_list, action_sequence

            elements_before = getattr(state, "indexed_elements", None)
            before_len = len(elements_before) if isinstance(elements_before, list) else 0
            pre_screenshot_bytes, xml_list, action_sequence = await self._execute_and_record_action(
                name,
                args,
                tc_id,
                state,
                messages,
                raw_text,
                step_token_usage,
                pre_screenshot_bytes,
                xml_list,
                action_sequence,
                turn,
                injected=injected,
                native_text=native_text,
                index_elements=index_elements,
                skip_observe=skip_observe,
                # Point taps after the burst's first device action re-check the live
                # target: the screen may have changed under the turn's screenshot.
                verify_live_target=actions_executed_in_burst > 0,
            )
            if is_wait_tool:
                has_waited = True
                index_elements = getattr(state, "indexed_elements", None) or index_elements
                actions_executed_in_burst = 0
                last_wait_text = str(turn.actions[-1][2]) if turn.actions else ""
                timed_out_signal = "[TIMEOUT_REACHED]" in last_wait_text
                # The shared action server skipped the wait because this turn's own
                # actions changed nothing (artemis/mcp/screen_effect.py): whatever
                # the burst planned next assumed an effect that did not happen.
                no_effect_signal = NO_EFFECT_MARKER in last_wait_text
                if (timed_out_signal or no_effect_signal) and not is_last:
                    reason = (
                        "the preceding action(s) had no visible effect"
                        if no_effect_signal
                        else "the previous wait reached its timeout without the event"
                    )
                    logger.info(f"Wait tool '{name}': {reason}. Aborting remaining burst actions.")
                    for rem_tc in tool_calls[idx + 1:]:
                        rem_id = rem_tc.get("id") or str(uuid.uuid4())
                        rem_name = rem_tc.get("name", "").split(":")[-1]
                        messages.append(
                            ToolMessage(
                                tool_call_id=rem_id,
                                name=rem_name,
                                content=f"Action not executed: {reason}; returning to AI for evaluation.",
                                status="error" if no_effect_signal else "success",
                            )
                        )
                    break
            elif name in action_names:
                actions_executed_in_burst += 1
                if args.get("target_text"):
                    # An action targeting target_text may have altered the screen; mark has_waited
                    # so subsequent actions with stale index/coordinates are safely stopped.
                    has_waited = True
                    index_elements = getattr(state, "indexed_elements", None) or index_elements
            elif name not in action_names:
                index_elements = self._extend_index_snapshot(state, index_elements, before_len)

            # Safety check: if action failed mid-burst, abort remaining actions to prevent blind execution
            if turn.actions and turn.actions[-1][1] in ("error", "failure") and not is_last:
                logger.warning(
                    f"Action '{name}' failed mid-burst (call {idx+1}/{total_calls}). "
                    f"Aborting remaining burst actions to prevent blind execution."
                )
                if skip_observe:
                    try:
                        obs, shot_bytes = await observe(self.ctx, self.controller, settle_ms=0)
                        self._raise_if_device_offline(obs=obs)
                        if obs.ok:
                            pre_screenshot_bytes = shot_bytes
                            self._rebase_screen_baseline(obs, shot_bytes)
                            if obs.elements_text:
                                xml_list = obs.elements_text
                            if state is not None:
                                if obs.hierarchy_ok:
                                    state.indexed_points = [el["center"] for el in obs.elements]
                                    state.indexed_elements = obs.elements
                                if obs.screenshot_path:
                                    state.latest_screenshot = obs.screenshot_path
                    except DeviceOfflineError:
                        raise
                    except Exception as shot_err:
                        logger.warning(f"Failed to capture fallback screen after failed burst action: {shot_err}")

                for rem_tc in tool_calls[idx + 1:]:
                    rem_id = rem_tc.get("id") or str(uuid.uuid4())
                    rem_name = rem_tc.get("name", "").split(":")[-1]
                    messages.append(
                        ToolMessage(
                            tool_call_id=rem_id,
                            name=rem_name,
                            content="Action skipped: Burst was aborted due to previous action failure.",
                            status="error",
                        )
                    )
                break
        return None, pre_screenshot_bytes, xml_list, action_sequence

    @staticmethod
    def _turn_index_snapshot(state: State) -> list[dict]:
        """The element list this turn's index targets resolve against.

        The state's list object itself, so in-place appends (explorer
        candidates) before any action join the snapshot; an empty list is
        installed on the state when there is none, for the same reason.
        """
        elements = getattr(state, "indexed_elements", None)
        if not isinstance(elements, list):
            elements = []
            try:
                state.indexed_elements = elements
            except (AttributeError, TypeError, ValueError):
                pass
        return elements

    @staticmethod
    def _extend_index_snapshot(state: State, snapshot: list[dict], before_len: int) -> list[dict]:
        """Re-takes the snapshot after a helper tool appended candidates to a
        list that is no longer the snapshot (an action replaced it first).

        Appends into the snapshot itself need nothing: the explorer numbered
        them against the snapshot. Only growth of a different list object
        moves the snapshot, since that is the list the new numbers refer to.
        """
        current = getattr(state, "indexed_elements", None)
        if not isinstance(current, list) or current is snapshot:
            return snapshot
        if len(current) > before_len:
            return current
        return snapshot

    async def _probe_unseen_content(
        self,
        args: dict,
        tc_id: str,
        state: State,
        messages: list[BaseMessage],
        raw_text: str,
        step_token_usage: dict,
        pre_screenshot_bytes,
        xml_list,
        action_sequence: int,
        turn: "_TurnRecord",
        native_text: str | None = None,
    ):
        """Before accepting "completed" on a scrollable screen, scroll once.

        Prompt rules alone do not make a model read a whole form or checkout
        page: a pinned bottom button looks like the end of it. So the system
        swipes the page up once itself. If the screen changes, there was unseen
        content: the report is deferred (returns the new screen state) and the
        model reviews it. If nothing moves, the page was already at its end and
        the swipe is dropped from the conversation (returns None).
        """
        if str(args.get("status", "")).lower() != "completed":
            return None
        if getattr(state, "screen_scrollable", None) is not True:
            return None
        probes = getattr(self, "_scroll_probes", 0)
        if probes >= _MAX_SCROLL_PROBES:
            return None
        self._scroll_probes = probes + 1

        from artemis.utils.image_hash import dhash_hex, hamming_distance_hex

        before_hash = dhash_hex(pre_screenshot_bytes)
        mark = len(messages)
        new_bytes, new_xml, new_sequence = await self._execute_and_record_action(
            "swipe",
            {"direction": "up"},
            tc_id,
            state,
            messages,
            "[system] Checking for unseen content below before accepting completion.",
            step_token_usage,
            pre_screenshot_bytes,
            xml_list,
            action_sequence,
            turn,
            native_text=native_text,
        )
        outcome = " ".join(
            str(getattr(m, "content", "")) for m in messages[mark:] if isinstance(m, ToolMessage)
        )
        del messages[mark:]
        distance = hamming_distance_hex(before_hash, dhash_hex(new_bytes))
        if distance is None or distance < _SCROLL_CHANGE_MIN_DISTANCE:
            logger.info("Scroll probe: page already at its end; accepting the report.")
            return None
        logger.info(
            f"Scroll probe: more content below (screen hash distance {distance}); deferring report."
        )
        messages.append(
            ToolMessage(
                tool_call_id=tc_id,
                name="report_task_status",
                content=f"{_MORE_BELOW_NOTICE} ({outcome.strip()})",
                status="error",
            )
        )
        return new_bytes, new_xml, new_sequence

    def _step_timing(self, exec_result) -> dict:
        """Timing for one recorded step: the turn's model time goes on the
        turn's first step only (several actions can share one model call)."""
        meta = getattr(exec_result, "metadata", None) or {}
        timing = {
            k: meta.get(k) for k in ("action_s", "observe_s") if meta.get(k) is not None
        }
        if getattr(self, "_turn_timing_pending", False):
            timing.update(getattr(self, "_turn_timing", {}) or {})
            self._turn_timing_pending = False
        else:
            timing["shared_model_call"] = True
        self._turn_action_s = getattr(self, "_turn_action_s", 0.0) + float(
            timing.get("action_s") or 0.0
        )
        self._turn_observe_s = getattr(self, "_turn_observe_s", 0.0) + float(
            timing.get("observe_s") or 0.0
        )
        return timing

    def _start_turn_record(self, turn: int, response, xml_list: str | None) -> None:
        """Opens this turn's ``turns.jsonl`` record (no-op unless enabled)."""
        self._turn_rec = None
        if not turn_log.turn_log_enabled():
            return
        t = getattr(self, "_turn_timing", {}) or {}
        model = getattr(self._llm, "model", None) or getattr(self._llm, "model_name", None)
        if not isinstance(model, str):
            try:
                model = (getattr(self.ctx, "llm_config", None) or get_default_llm_config()).get_agent("operator").model
            except Exception:  # pylint: disable=broad-exception-caught
                model = None
        self._turn_rec = {
            "turn": turn,
            "model": model,
            "llm_s": t.get("llm_s"),
            "prep_s": t.get("prep_s"),
            **turn_log.usage_fields(response),
            "list_chars": len(xml_list or ""),
            "list_format": "compact" if getattr(self, "compact_list", False) else "full",
            "image_max_side": getattr(getattr(self, "model_image_cfg", None), "max_side", None),
            "media_resolution": getattr(self, "media_resolution", None),
            "actions": [],
        }

    def _write_turn_record(self, timing: dict, total_s: float, action_s: float, observe_s: float) -> None:
        """Closes and appends this turn's record."""
        rec = getattr(self, "_turn_rec", None)
        self._turn_rec = None
        if rec is None:
            return
        rec.update(
            total_s=round(total_s, 3),
            action_s=round(action_s, 3),
            observe_s=round(observe_s, 3),
            other_s=round(max(0.0, total_s - (timing.get("llm_s") or 0) - action_s - observe_s
                              - (timing.get("prep_s") or 0)), 3),
        )
        engine = getattr(self.ctx, "data_engine", None)
        directory = getattr(engine, "base_dir", None) if engine is not None else None
        if directory is None:
            from artemis.config.paths import get_traces_dir

            directory = get_traces_dir() / "flash_turns"
        path = turn_log.append_turn(directory, rec)
        if path and rec.get("turn") == 1:
            logger.info(f"Turn log: {path}")

    def _log_turn_timing(self, turn: int, total_s: float) -> None:
        """One log line per turn: where the time went."""
        t = getattr(self, "_turn_timing", {}) or {}
        action_s = getattr(self, "_turn_action_s", 0.0)
        observe_s = getattr(self, "_turn_observe_s", 0.0)
        self._turn_action_s = self._turn_observe_s = 0.0
        self._write_turn_record(t, total_s, action_s, observe_s)
        agy = t.get("agy") or {}
        agy_note = (
            f" (agy: {agy.get('model_calls')} model calls,"
            f" screenshot {'opened' if agy.get('opened_screenshot') else 'skipped'})"
            if agy
            else ""
        )
        logger.info(
            f"⏱ Turn {turn}: total {total_s:.1f}s = model {t.get('llm_s', 0):.1f}s{agy_note}"
            f" + actions {action_s:.1f}s + screen capture {observe_s:.1f}s"
            f" + prompt prep {t.get('prep_s', 0):.1f}s"
        )

    async def _reobserve_after_resume(self, state: State):
        """Captures the live screen after a paused LLM call was resumed."""
        shot_path, img_bytes, xml_list = await capture_screenshot_and_parse_ui(
            self.ctx, state, self.controller, skip_settling=False
        )
        state.latest_screenshot = shot_path
        return img_bytes, xml_list

    async def _prepare_conversation(self, state: State, tools_declaration: list):
        """Installs the static prefix and captures the initial device state."""
        ledger = self._build_ledger()
        ledger.set_static_prefix(
            [SystemMessage(content=self._render_system_prompt(tools_declaration))]
        )

        # Capture Initial State (Screenshot + UI Tree)
        shot_path, img_bytes, xml_list = await capture_screenshot_and_parse_ui(
            self.ctx, state, self.controller, skip_settling=False
        )
        state.latest_screenshot = shot_path
        return ledger, img_bytes, xml_list

    @staticmethod
    def _commit_turn(ledger: TranscriptLedger, turn: _TurnRecord | None) -> None:
        """Commits the previous turn once its step ids and outcomes are known."""
        if turn is None:
            return
        ledger.commit_staged(
            step_key=turn.step_keys[0] if turn.step_keys else None,
            validator_result=turn.result(),
            extra_step_keys=turn.step_keys[1:],
        )

    @trace(type="agent", name="FlashRunner")
    async def run(self, state: State) -> dict:
        limit = self.turn_limit
        logger.info(
            f"Starting Artemis Flash reactive loop for goal: {self.goal}"
            f" (turn limit: {limit if limit else 'unlimited'})"
        )

        session_id = getattr(getattr(self.ctx, "data_engine", None), "current_session_id", None)
        session_dir = getattr(getattr(self.ctx, "data_engine", None), "base_dir", None)
        log_session_start(
            session_id=str(session_id) if session_id else None,
            goal=self.goal,
            session_dir=session_dir,
        )

        # 1. Initialize Universal LLM via Service Layer
        llm = self._init_llm()
        self._llm = llm

        tools_declaration = self._get_tools()
        report_only_tools = [t for t in tools_declaration if t.name == "report_task_status"]

        try:
            return await self._run_reactive_loop(
                state, llm, tools_declaration, report_only_tools, limit
            )
        except DeviceOfflineError as offline_err:
            logger.error(f"FlashRunner stopping due to device disconnect: {offline_err}")
            await self._flush_history_jobs()
            return {"status": "failed", "explanation": str(offline_err)}

    async def _run_reactive_loop(
        self,
        state: State,
        llm: Any,
        tools_declaration: list,
        report_only_tools: list,
        limit: int | None,
    ) -> dict:
        ledger, img_bytes, xml_list = await self._prepare_conversation(state, tools_declaration)

        turns = 0
        action_sequence = 0
        final_report = None
        current_pre_screenshot_bytes = img_bytes
        current_xml_list = xml_list
        previous_turn: _TurnRecord | None = None
        pending_notices: list[str] = []
        silent_turns = 0

        while limit is None or turns < limit:
            turns += 1
            logger.info(f"--- Artemis Flash Turn {turns}{f'/{limit}' if limit else ''} ---")

            turn_started = time.monotonic()
            if self.ctx.data_engine:
                self.ctx.data_engine.allocate_step_id()

            # Commit the previous turn: its step ids and outcomes exist now.
            self._commit_turn(ledger, previous_turn)
            previous_turn = None

            # Tool restriction on the final turn (bounded loops only)
            is_final = limit is not None and turns == limit
            current_tools = report_only_tools if is_final else tools_declaration
            previous_turn_silent = bool(getattr(ledger, "last_turn_silent", False))
            injected: str | None = None
            injected_texts: list[str] = []
            guidance = None
            llm_s = 0.0
            prep_s = 0.0
            resumes = 0

            while True:
                prep_started = time.monotonic()
                # Check for real-time injected instructions: the verbatim text
                # is stamped on the step, the guidance pair goes to the tail.
                new_injected, injected_guidance = await self._read_injected_instruction(
                    ledger.elapsed_label()
                )
                if new_injected:
                    injected_texts.append(new_injected)
                    injected = "\n".join(injected_texts)
                # Guidance read before a pause still applies to the re-asked turn.
                guidance = injected_guidance or guidance

                tail = self._build_tail(
                    ledger,
                    turns,
                    current_pre_screenshot_bytes,
                    current_xml_list,
                    injected=guidance,
                    notices=pending_notices,
                    is_final=is_final,
                    # The ledger judged the turn it just committed: a bare tool
                    # call (or thinking only) earns a one-line reminder, no bounce.
                    previous_turn_silent=previous_turn_silent,
                    scrollable=getattr(state, "screen_scrollable", None),
                )
                pending_notices = []

                # S + F + A + tail: chunk compression and the scrub edge advance here.
                messages = ledger.render([tail])
                turn_base = len(messages) - 1

                # Log Android Output & AI Input if ARTEMIS_DEBUG_AI_IO is enabled
                session_id = getattr(getattr(self.ctx, "data_engine", None), "current_session_id", None)
                session_dir = getattr(getattr(self.ctx, "data_engine", None), "base_dir", None)
                log_android_output(
                    width=getattr(getattr(self.ctx, "device", None), "device_width", None),
                    height=getattr(getattr(self.ctx, "device", None), "device_height", None),
                    screenshot_bytes=current_pre_screenshot_bytes,
                    xml_list=current_xml_list,
                    scrollable=getattr(state, "screen_scrollable", None),
                    turn=turns,
                    session_id=str(session_id) if session_id else None,
                    session_dir=session_dir,
                )
                log_ai_input(
                    messages=messages,
                    tools=current_tools,
                    turn=turns,
                    session_id=str(session_id) if session_id else None,
                    session_dir=session_dir,
                )

                llm_started = time.monotonic()
                prep_s += llm_started - prep_started
                try:
                    response = await self._invoke_model(llm, current_tools, messages)
                    llm_s += time.monotonic() - llm_started
                    break
                except LLMResumedError:
                    llm_s += time.monotonic() - llm_started
                    resumes += 1
                    # The task sat paused: the device may have moved on (app
                    # closed, another screen). Observe it again and re-ask
                    # instead of acting on the pre-pause screenshot.
                    logger.info("Resumed after LLM pause; re-observing the device.")
                    (
                        current_pre_screenshot_bytes,
                        current_xml_list,
                    ) = await self._reobserve_after_resume(state)
                    pending_notices = [_RESUMED_NOTICE]

            if response is None:
                break

            # Where this turn's time went; stamped on the turn's first step.
            self._turn_timing = {
                "prep_s": round(prep_s, 3),
                "llm_s": round(llm_s, 3),
                "llm_resumes": resumes,
            }
            agy_profile = (getattr(response, "response_metadata", None) or {}).get("agy")
            if agy_profile:
                self._turn_timing["agy"] = agy_profile
            self._turn_timing_pending = True

            messages.append(response)

            # Log AI Output if ARTEMIS_DEBUG_AI_IO is enabled
            session_id = getattr(getattr(self.ctx, "data_engine", None), "current_session_id", None)
            session_dir = getattr(getattr(self.ctx, "data_engine", None), "base_dir", None)
            log_ai_output(
                response=response,
                latency_s=self._turn_timing.get("llm_s"),
                turn=turns,
                session_id=str(session_id) if session_id else None,
                session_dir=session_dir,
            )

            # Extract thought and tool calls
            raw_text = self._extract_response_text(response)
            native_text = self._extract_response_thinking(response)

            # Extract token usage metadata from response
            step_token_usage, measured = self._resolve_token_usage(response, messages, raw_text)
            # The ledger's compaction thresholds read the runner's own prompt
            # size, never the session-wide meter (which any agent overwrites).
            # The prompt as sent (``messages`` minus the just-appended reply)
            # rides along so the ledger can calibrate its chars-per-token
            # ratio — only from a provider measurement, never from the
            # estimate (same as the Pro operator).
            ledger.record_prompt_tokens(
                step_token_usage.get("prompt_tokens"),
                messages=messages[:-1] if measured else None,
            )

            self._record_llm_trace(step_token_usage, raw_text)
            self._start_turn_record(turns, response, current_xml_list)

            tool_calls = self._resolve_tool_calls(response, raw_text)

            if not tool_calls:
                logger.info(
                    f"FlashRunner received response without tool calls at turn {turns}:"
                    f" {raw_text[:100]}..."
                )
                if is_final:
                    final_report = {"status": "failed", "explanation": raw_text}
                    break
                silent_turns += 1
                max_silent = getattr(self, "max_silent_turns", 5) or 0
                if max_silent and silent_turns >= max_silent:
                    stop_reason = (getattr(response, "response_metadata", None) or {}).get(
                        "stop_reason"
                    )
                    logger.warning(
                        f"FlashRunner stopping: {silent_turns} consecutive turns without a tool call"
                        f" (last stop_reason={stop_reason!r})."
                    )
                    final_report = {
                        "status": "failed",
                        "explanation": (
                            f"The model called no tool for {silent_turns} consecutive turns"
                            f" (last stop_reason={stop_reason!r}, last reply {raw_text[:200]!r});"
                            " stopped instead of looping."
                        ),
                    }
                    break
                pending_notices.append(_NO_TOOL_CALL_NOTICE)
                ledger.stage_turn(messages[turn_base:])
                previous_turn = _TurnRecord()
                continue

            silent_turns = 0
            # Process tool calls
            turn = _TurnRecord()
            (
                final_report_from_calls,
                current_pre_screenshot_bytes,
                current_xml_list,
                action_sequence,
            ) = await self._process_tool_calls(
                tool_calls,
                state,
                messages,
                raw_text,
                step_token_usage,
                current_pre_screenshot_bytes,
                current_xml_list,
                action_sequence,
                turn,
                injected=injected,
                native_text=native_text,
            )
            ledger.stage_turn(messages[turn_base:])
            previous_turn = turn
            self._log_turn_timing(turns, time.monotonic() - turn_started)
            if final_report_from_calls is not None:
                return final_report_from_calls

        await self._flush_history_jobs()

        if final_report is None:
            explanation = (
                "Max turns reached without final status report."
                if limit is not None and turns >= limit
                else "The model returned no response; the reactive loop stopped."
            )
            final_report = {"status": "failed", "explanation": explanation}
        return final_report
