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

"""Human-in-the-loop choice support for the Flash runner.

Provides the ``ask_user_choice`` tool declaration, atomic file-based IPC
between the runner and UI/MCP (``pending_choice.json``, ``choice_answer.json``,
``user_choice_mode.json``), and the unbounded async wait loop with clean
cancellation, SSE event publishing, and ``status.json`` synchronization.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from artemis.core.tool_declaration import ToolDeclaration
from artemis.graph.perception import _check_injected_instruction_file
from artemis.runtime import trace_store
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

PENDING_CHOICE_FILENAME = "pending_choice.json"
CHOICE_ANSWER_FILENAME = "choice_answer.json"
USER_CHOICE_MODE_FILENAME = "user_choice_mode.json"

_cli_choice_prompt_handler: Callable[[dict[str, Any]], str | None] | None = None


def set_cli_choice_prompt(handler: Callable[[dict[str, Any]], str | None] | None) -> None:
    """Register (or clear) an interactive CLI prompt callback for standalone TTY runs."""
    global _cli_choice_prompt_handler
    _cli_choice_prompt_handler = handler

SYSTEM_AUTO_LABEL_PICK = "Để AI tự chọn"
SYSTEM_AUTO_LABEL_STUCK = "Cho AI thử các hướng trên"
SYSTEM_CUSTOM_LABEL = "Yêu cầu khác…"
SYSTEM_MANUAL_LABEL = "Tôi tự thao tác trên điện thoại"

ASK_USER_CHOICE_TOOL = ToolDeclaration(
    name="ask_user_choice",
    description=(
        "[ASK USER] Let the user decide instead of guessing or tapping at random. "
        "Ends your turn and waits for the answer. "
        "kind='pick_item': the screen offers several comparable items of the same kind "
        "and the goal does not say which one (no name, variant, or rule such as "
        "cheapest/first/best rated). "
        "kind='stuck': you cannot tell how to continue toward the goal (target not "
        "found, unfamiliar screen, several plausible paths, taps with no effect); "
        "list the next steps you consider. "
        "kind='form_options': a group of selectable options in a form (radio buttons = "
        "choose one, checkboxes = choose several; e.g. size, sugar level, toppings, add-ons) "
        "and the goal does not say which to pick; list EVERY option of the group (scroll to "
        "reveal hidden ones) and set multi_select=true for checkboxes. "
        "Never for commitment actions (pay, order, delete, send). "
        "Never re-ask what the user just answered."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["pick_item", "stuck", "form_options"]},
            "multi_select": {
                "type": "boolean",
                "description": (
                    "form_options only: true when the user may pick several options "
                    "(checkboxes), false for a radio group (pick one)."
                ),
            },
            "max_select": {
                "type": "integer",
                "description": "form_options only: the most options allowed, when the screen states a limit.",
            },
            "question": {
                "type": "string",
                "description": "One short question in the user's language.",
            },
            "options": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {
                            "type": "string",
                            "description": (
                                "pick_item/form_options: the name as shown on screen. "
                                "stuck: a short next step."
                            ),
                        },
                        "detail": {
                            "type": "string",
                            "description": (
                                "Facts that tell options apart: price, extra cost, rating, "
                                "distance, shop, already selected, why this step…"
                            ),
                        },
                        "index": {
                            "type": "integer",
                            "description": "Element index in the current list, if visible.",
                        },
                    },
                    "required": ["label"],
                },
            },
            "reason": {
                "type": "string",
                "description": "Why you cannot decide this yourself.",
            },
        },
        "required": ["kind", "question", "options"],
    },
)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` atomically via temp file + fsync + replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON dict from ``path``, returning ``None`` if missing or invalid."""
    try:
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def normalize_choice_label(label: str) -> str:
    """Normalize an option label for anti-repeat comparison."""
    text = str(label or "").replace("…", "").replace("...", "")
    return " ".join(text.strip().lower().split())


def choice_signature(kind: str, options: list[dict[str, Any]]) -> tuple[str, frozenset[str]]:
    """Return the deduplication signature ``(kind, normalized labels)``."""
    return (
        str(kind or "").strip().lower(),
        frozenset(
            normalize_choice_label(opt.get("label", ""))
            for opt in options
            if isinstance(opt, dict) and normalize_choice_label(opt.get("label", ""))
        ),
    )


def validate_ask_user_choice_args(
    args: dict[str, Any],
    *,
    max_options: int = 8,
    max_form_options: int = 20,
) -> tuple[bool, str, dict[str, Any]]:
    """Validate and normalize tool arguments for ``ask_user_choice``."""
    kind = str(args.get("kind") or "").strip().lower()
    if kind not in ("pick_item", "stuck", "form_options"):
        return False, "Invalid 'kind': must be 'pick_item', 'stuck' or 'form_options'.", {}
    if kind == "form_options":
        # A form group is shown in full: the user needs every option, not a sample.
        max_options = max(max_options, max_form_options)

    question = str(args.get("question") or "").strip()
    if not question:
        return False, "Invalid 'question': must be a non-empty string.", {}

    raw_options = args.get("options")
    if not isinstance(raw_options, list) or not raw_options:
        return False, "Invalid 'options': must be a non-empty list.", {}

    cleaned_options: list[dict[str, Any]] = []
    for i, opt in enumerate(raw_options[:max_options], start=1):
        if not isinstance(opt, dict):
            return False, f"Invalid option at position {i}: must be an object.", {}
        label = str(opt.get("label") or "").strip()
        if not label:
            return False, f"Invalid option at position {i}: 'label' must be non-empty.", {}
        item: dict[str, Any] = {"id": str(i), "label": label}
        detail = str(opt.get("detail") or "").strip()
        if detail:
            item["detail"] = detail
        idx_val = opt.get("index")
        if isinstance(idx_val, int) and not isinstance(idx_val, bool):
            item["index"] = idx_val
        cleaned_options.append(item)

    if kind == "pick_item" and len(cleaned_options) < 2:
        return (
            False,
            "Invalid 'options' for kind='pick_item': at least 2 comparable options are required.",
            {},
        )
    if len(cleaned_options) < 1:
        return False, "Invalid 'options': at least 1 option is required.", {}

    reason = str(args.get("reason") or "").strip()
    normalized = {
        "kind": kind,
        "question": question,
        "options": cleaned_options,
        "reason": reason,
    }
    if kind == "form_options":
        normalized["multi_select"] = _as_bool(args.get("multi_select"))
        max_sel = args.get("max_select")
        if isinstance(max_sel, int) and not isinstance(max_sel, bool) and max_sel >= 1:
            normalized["max_select"] = max_sel
    return True, "", normalized


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "1", "yes")


def build_pending_choice_payload(
    *,
    choice_id: str,
    session_id: str | None,
    kind: str,
    question: str,
    options: list[dict[str, Any]],
    reason: str = "",
    screenshot_path: str | None = None,
    created_at: float | None = None,
    multi_select: bool = False,
    max_select: int | None = None,
) -> dict[str, Any]:
    """Assemble ``pending_choice.json`` with system-appended options."""
    auto_label = SYSTEM_AUTO_LABEL_STUCK if kind == "stuck" else SYSTEM_AUTO_LABEL_PICK
    full_options = [dict(opt) for opt in options]
    full_options.extend(
        [
            {"id": "auto", "label": auto_label, "kind": "auto"},
            {"id": "custom", "label": SYSTEM_CUSTOM_LABEL, "kind": "custom"},
            {"id": "manual", "label": SYSTEM_MANUAL_LABEL, "kind": "manual"},
        ]
    )
    payload: dict[str, Any] = {
        "choice_id": choice_id,
        "session_id": session_id,
        "kind": kind,
        "question": question,
        "options": full_options,
        "reason": reason,
        "screenshot_path": screenshot_path,
        "created_at": time.time() if created_at is None else float(created_at),
    }
    if kind == "form_options":
        payload["multi_select"] = bool(multi_select)
        if max_select:
            payload["max_select"] = int(max_select)
    return payload


def read_user_choice_mode(
    trace_dir: str | Path | None,
    *,
    default_ask_on_choice: bool = True,
    default_ask_when_stuck: bool = True,
) -> tuple[bool, bool]:
    """Read ``user_choice_mode.json`` from ``trace_dir``, falling back to defaults."""
    if not trace_dir:
        return bool(default_ask_on_choice), bool(default_ask_when_stuck)
    data = _read_json(Path(trace_dir) / USER_CHOICE_MODE_FILENAME)
    if not data:
        return bool(default_ask_on_choice), bool(default_ask_when_stuck)
    ask_on_choice = data.get("ask_on_choice")
    ask_when_stuck = data.get("ask_when_stuck")
    return (
        bool(ask_on_choice) if isinstance(ask_on_choice, bool) else bool(default_ask_on_choice),
        bool(ask_when_stuck) if isinstance(ask_when_stuck, bool) else bool(default_ask_when_stuck),
    )


def write_user_choice_mode(
    trace_dir: str | Path,
    *,
    ask_on_choice: bool,
    ask_when_stuck: bool,
) -> dict[str, Any]:
    """Atomically write ``user_choice_mode.json`` into ``trace_dir``."""
    payload = {
        "ask_on_choice": bool(ask_on_choice),
        "ask_when_stuck": bool(ask_when_stuck),
        "updated_at": time.time(),
    }
    _atomic_write_json(Path(trace_dir) / USER_CHOICE_MODE_FILENAME, payload)
    return payload


def read_pending_choice(trace_dir: str | Path | None) -> dict[str, Any] | None:
    """Read ``pending_choice.json`` if present."""
    if not trace_dir:
        return None
    return _read_json(Path(trace_dir) / PENDING_CHOICE_FILENAME)


def write_choice_answer(trace_dir: str | Path, answer: dict[str, Any]) -> None:
    """Atomically write ``choice_answer.json`` into ``trace_dir``."""
    _atomic_write_json(Path(trace_dir) / CHOICE_ANSWER_FILENAME, answer)


def clear_user_choice_files(trace_dir: str | Path | None) -> None:
    """Remove ``pending_choice.json`` and ``choice_answer.json`` from ``trace_dir``."""
    if not trace_dir:
        return
    base = Path(trace_dir)
    _safe_unlink(base / PENDING_CHOICE_FILENAME)
    _safe_unlink(base / CHOICE_ANSWER_FILENAME)


def compact_pending_choice(pending: dict[str, Any]) -> dict[str, Any]:
    """Return the compact summary stored in ``status.json`` / ``active_tasks``."""
    created_at = pending.get("created_at")
    waiting_seconds = (
        round(max(0.0, time.time() - float(created_at)), 1)
        if isinstance(created_at, (int, float))
        else 0.0
    )
    return {
        "choice_id": pending.get("choice_id"),
        "kind": pending.get("kind"),
        "question": pending.get("question"),
        "options": pending.get("options", []),
        "reason": pending.get("reason", ""),
        "multi_select": bool(pending.get("multi_select", False)),
        "max_select": pending.get("max_select"),
        "created_at": created_at,
        "waiting_seconds": waiting_seconds,
    }


def update_status_user_choice(
    session_id: str | None,
    pending: dict[str, Any] | None,
) -> None:
    """Update ``status.json`` with ``awaiting_user`` ONLY if ``status.json`` already exists.

    Storage ``status`` is intentionally kept untouched (remains ``"running"``) so
    liveness checks, stop, and instruction injection continue to work normally.
    """
    if not session_id:
        return
    sid = str(session_id)
    try:
        existing = trace_store.read_status(sid)
        if not existing:
            return
        if pending is not None:
            trace_store.update_trace_fields(
                sid,
                awaiting_user=True,
                pending_choice=compact_pending_choice(pending),
            )
        else:
            trace_store.update_trace_fields(
                sid,
                awaiting_user=False,
                pending_choice=None,
            )
    except Exception as exc:
        logger.debug(f"Could not update awaiting_user in status.json for {sid}: {exc}")


def build_needs_input_message(trace_id: str, pending: dict[str, Any]) -> str:
    """Build a self-contained notification message for MCP/external notifiers."""
    choice_id = pending.get("choice_id", "")
    kind = pending.get("kind", "pick_item")
    question = pending.get("question", "")
    lines = [
        f"ARTEMIS needs your input (trace_id: {trace_id}, choice_id: {choice_id}, kind: {kind}).",
        "",
        f"Question: {question}",
        "",
        "Options:",
    ]
    for opt in pending.get("options") or []:
        if not isinstance(opt, dict):
            continue
        oid = opt.get("id", "")
        label = opt.get("label", "")
        detail = opt.get("detail")
        if detail:
            lines.append(f"  - {oid}: {label} — {detail}")
        else:
            lines.append(f"  - {oid}: {label}")

    auto_note = (
        '("auto" = let AI try ONLY the listed steps above within a fixed turn budget)'
        if kind == "stuck"
        else '("auto" = let AI pick the best option for the goal)'
    )
    lines.extend(
        [
            "",
            f"Ask the user to choose, then call "
            f'mobile_manage_task(action="answer_choice", trace_id="{trace_id}", '
            f'instruction="<option number | auto | manual | custom text>"). {auto_note}'
            + (" Checkbox group: several numbers allowed, e.g. \"1,3\"." if pending.get("multi_select") else ""),
        ]
    )
    return "\n".join(lines)


def _dispatch_needs_input_notification(
    session_id: str | None,
    pending: dict[str, Any],
) -> None:
    """Best-effort dispatch of ``needs_input`` via MCP notifiers when running under MCP."""
    if not session_id:
        return
    sid = str(session_id)
    conv_id: str | None = os.getenv("ARTEMIS_CONVERSATION_ID") or None
    if not conv_id:
        try:
            status_data = trace_store.read_status(sid)
            if isinstance(status_data, dict):
                raw_conv = status_data.get("conversation_id")
                if raw_conv:
                    conv_id = str(raw_conv)
        except Exception:
            conv_id = None
    if not conv_id or conv_id.lower() in ("default", "none", ""):
        return
    try:
        from mcp_server.notifiers import notify

        notify(
            conversation_id=conv_id,
            message=build_needs_input_message(sid, pending),
            title="Artemis Needs Input",
            event_type="needs_input",
            payload={
                "trace_id": sid,
                "choice_id": pending.get("choice_id"),
                "kind": pending.get("kind"),
                "question": pending.get("question"),
                "options": pending.get("options", []),
            },
        )
    except Exception as exc:
        logger.debug(f"needs_input notification skipped for {sid}: {exc}")


def _publish_choice_event(
    data_engine: Any,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Publish SSE event via ``data_engine._publish`` if available."""
    if data_engine is None:
        return
    publish = getattr(data_engine, "_publish", None)
    if callable(publish):
        try:
            publish(event_type, payload)
        except Exception as exc:
            logger.debug(f"Failed to publish {event_type}: {exc}")


def _validate_answer_for_pending(
    answer: dict[str, Any],
    pending: dict[str, Any],
) -> dict[str, Any] | None:
    """Return normalized answer dict if ``answer`` matches ``pending``, else ``None``."""
    expected_id = pending.get("choice_id")
    if answer.get("choice_id") != expected_id:
        return None
    ans_type = str(answer.get("type") or "").strip().lower()
    if ans_type == "option":
        opt_id = str(answer.get("option_id") or "").strip()
        for opt in pending.get("options") or []:
            if isinstance(opt, dict) and str(opt.get("id")) == opt_id and "kind" not in opt:
                return {
                    "choice_id": expected_id,
                    "type": "option",
                    "option_id": opt_id,
                    "label": opt.get("label", ""),
                    "detail": opt.get("detail", ""),
                }
        return None
    if ans_type == "options":
        return _validate_multi_answer(answer, pending)
    if ans_type == "auto":
        return {"choice_id": expected_id, "type": "auto"}
    if ans_type == "custom":
        text = str(answer.get("text") or "").strip()
        if not text:
            return None
        return {"choice_id": expected_id, "type": "custom", "text": text}
    if ans_type == "manual":
        return {"choice_id": expected_id, "type": "manual"}
    return None


def _validate_multi_answer(answer: dict[str, Any], pending: dict[str, Any]) -> dict[str, Any] | None:
    """Several options of a checkbox group (``type='options'``), else None."""
    if not pending.get("multi_select"):
        return None
    raw_ids = answer.get("option_ids")
    if not isinstance(raw_ids, list):
        return None
    by_id = {
        str(o.get("id")): o
        for o in (pending.get("options") or [])
        if isinstance(o, dict) and "kind" not in o
    }
    ids: list[str] = []
    for rid in raw_ids:
        sid = str(rid).strip()
        if sid not in by_id:
            return None
        if sid not in ids:
            ids.append(sid)
    if not ids:
        return None
    max_sel = pending.get("max_select")
    if isinstance(max_sel, int) and len(ids) > max_sel:
        return None
    return {
        "choice_id": pending.get("choice_id"),
        "type": "options",
        "option_ids": ids,
        "labels": [by_id[i].get("label", "") for i in ids],
        "details": [by_id[i].get("detail", "") for i in ids],
    }


async def wait_for_user_choice(
    *,
    trace_dir: str | Path,
    session_id: str | None,
    kind: str,
    question: str,
    options: list[dict[str, Any]],
    reason: str = "",
    screenshot_path: str | None = None,
    data_engine: Any = None,
    poll_interval_s: float = 0.5,
    multi_select: bool = False,
    max_select: int | None = None,
) -> tuple[dict[str, Any], float]:
    """Persist pending choice, emit SSE/notifications, and wait with no timeout.

    Returns ``(resolved_answer, user_wait_seconds)``. Propagates
    ``asyncio.CancelledError`` without swallowing it and always cleans up
    ``pending_choice.json``, ``choice_answer.json``, and ``status.json`` in
    ``finally``.
    """
    base = Path(trace_dir)
    base.mkdir(parents=True, exist_ok=True)
    choice_id = f"c_{uuid.uuid4().hex[:12]}"
    pending = build_pending_choice_payload(
        choice_id=choice_id,
        session_id=str(session_id) if session_id else None,
        kind=kind,
        question=question,
        options=options,
        reason=reason,
        screenshot_path=screenshot_path,
        multi_select=multi_select,
        max_select=max_select,
    )

    pending_path = base / PENDING_CHOICE_FILENAME
    answer_path = base / CHOICE_ANSWER_FILENAME

    # Remove any stale answer before publishing the new pending question.
    _safe_unlink(answer_path)
    _atomic_write_json(pending_path, pending)
    update_status_user_choice(session_id, pending)

    _publish_choice_event(
        data_engine,
        "user_choice_requested",
        {**pending, "session_id": str(session_id) if session_id else pending.get("session_id")},
    )
    _dispatch_needs_input_notification(session_id, pending)

    started = time.monotonic()
    resolved: dict[str, Any] | None = None
    cli_prompt_task: asyncio.Task | None = None
    cli_stop_event = threading.Event()
    cli_has_stop_event = False
    if _cli_choice_prompt_handler is not None:
        handler = _cli_choice_prompt_handler
        try:
            cli_has_stop_event = len(inspect.signature(handler).parameters) >= 2
        except (TypeError, ValueError):
            cli_has_stop_event = False

        async def _drive_cli_prompt() -> None:
            try:
                if cli_has_stop_event:
                    raw = await asyncio.to_thread(handler, pending, cli_stop_event)  # type: ignore[arg-type]
                else:
                    raw = await asyncio.to_thread(handler, pending)  # type: ignore[call-arg]
            except Exception as exc:
                logger.debug(f"CLI choice prompt ended with {exc}")
                return
            if raw is None or cli_stop_event.is_set():
                return
            parsed = parse_choice_input(raw, pending)
            if parsed is not None:
                await asyncio.to_thread(write_choice_answer, base, parsed)

        cli_prompt_task = asyncio.create_task(_drive_cli_prompt())

    try:
        while True:
            raw_answer = await asyncio.to_thread(_read_json, answer_path)
            if raw_answer is not None:
                validated = _validate_answer_for_pending(raw_answer, pending)
                if validated is not None:
                    resolved = validated
                    break
                # Ignore mismatched or invalid choice_answer.json (unlink stale answer)
                if raw_answer.get("choice_id") != choice_id:
                    logger.debug(
                        f"Ignoring stale choice_answer with choice_id={raw_answer.get('choice_id')!r}"
                    )
                    _safe_unlink(answer_path)

            injected = await asyncio.to_thread(_check_injected_instruction_file, str(base))
            if injected and str(injected.get("instruction") or "").strip():
                resolved = {
                    "choice_id": choice_id,
                    "type": "custom",
                    "text": str(injected["instruction"]).strip(),
                    "from_injected_instruction": True,
                    "release_loop": bool(injected.get("release_loop")),
                }
                break

            await asyncio.sleep(poll_interval_s)

        waited_s = round(max(0.0, time.monotonic() - started), 3)
        return resolved, waited_s
    finally:
        cli_stop_event.set()
        if cli_prompt_task is not None and not cli_prompt_task.done():
            cur_task = asyncio.current_task()
            being_cancelled = bool(cur_task is not None and cur_task.cancelling())
            if cli_has_stop_event and not being_cancelled:
                try:
                    await asyncio.wait_for(cli_prompt_task, timeout=1.0)
                except Exception:
                    pass
            if not cli_prompt_task.done():
                cli_prompt_task.cancel()
        clear_user_choice_files(base)
        update_status_user_choice(session_id, None)
        _publish_choice_event(
            data_engine,
            "user_choice_resolved",
            {
                "choice_id": choice_id,
                "session_id": str(session_id) if session_id else None,
                "kind": kind,
                "resolution": resolved or {"choice_id": choice_id, "type": "cancelled"},
            },
        )


def find_comparable_items(
    elements: list[dict[str, Any]] | None,
    width: int | None,
    height: int | None,
    *,
    min_items: int = 3,
    min_text_len: int = 12,
    min_width_ratio: float = 0.25,
) -> list[str]:
    """Texts of repeated, same-layout items (product cards, offers, result rows).

    Purely structural (app- and language-agnostic, AGENTS §2.7): accessibility
    text elements of the same class and the same width (2% of the screen
    width) that sit on at least two different rows. Short labels (tabs,
    buttons, prices) and narrow chips are ignored. Returns the texts of the
    largest such group with at least ``min_items`` distinct texts, else ``[]``.
    """
    if not elements:
        return []
    rights = [
        e["bounds"][2] for e in elements
        if isinstance(e, dict) and isinstance(e.get("bounds"), (list, tuple)) and len(e["bounds"]) == 4
    ]
    screen_w = int(width or 0) or (max(rights) if rights else 0)
    if screen_w <= 0:
        return []
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for el in elements:
        if not isinstance(el, dict) or el.get("is_ocr") or el.get("is_hint"):
            continue
        text = _clean_label(el.get("text"))
        bounds = el.get("bounds")
        if len(text) < min_text_len or not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            continue
        if not looks_like_identifier(text) and not _mostly_letters(text):
            continue  # a price / rating / sold-count line, not an item name
        left, top, right, bottom = bounds
        w = right - left
        if w < screen_w * min_width_ratio or bottom <= top:
            continue
        key = (str(el.get("class") or ""), int(round(w * 50 / screen_w)))
        groups.setdefault(key, []).append(el)

    def _readable_name(member: dict[str, Any]) -> str:
        """The member's own label, or for an id-only container the longest
        readable text drawn inside its bounds (the card's title)."""
        own = _clean_label(member.get("text"))
        if not looks_like_identifier(own):
            return own
        l0, t0, r0, b0 = member["bounds"]
        best_inner, best_letters = "", 0
        for other in elements:
            if other is member or not isinstance(other, dict):
                continue
            ob = other.get("bounds")
            txt = _clean_label(other.get("text"))
            if not isinstance(ob, (list, tuple)) or len(ob) != 4 or looks_like_identifier(txt):
                continue
            if not (l0 <= ob[0] and t0 <= ob[1] and ob[2] <= r0 and ob[3] <= b0) or not _mostly_letters(txt):
                continue
            # The title is the inner text with the most letters (not a rating,
            # price or sold-count line, which are mostly digits and symbols).
            letters = sum(ch.isalpha() for ch in txt)
            if letters > best_letters:
                best_inner, best_letters = txt, letters
        return best_inner if len(best_inner) >= min_text_len else ""

    best: list[str] = []
    for members in groups.values():
        rows = {int(round(m["bounds"][1] * 100 / max(1, int(height or 0) or screen_w * 2))) for m in members}
        if len(rows) < 2:
            continue
        texts: list[str] = []
        for m in members:
            t = _readable_name(m)
            # A member with no readable name (skeleton / still loading) is skipped:
            # never hint with developer ids or empty placeholders.
            if t and t not in texts:
                texts.append(t)
        if len(texts) >= min_items and len(texts) > len(best):
            best = texts
    return best


def _clean_label(text: Any) -> str:
    """Accessibility labels that join several texts with ``!`` runs, tidied."""
    import re

    return re.sub(r"\s+", " ", re.sub(r"!{1,}", " ", str(text or ""))).strip(" -–|")


def _mostly_letters(text: str) -> bool:
    """At least 60% of the visible characters are letters (names, not numbers)."""
    visible = [ch for ch in str(text or "") if not ch.isspace()]
    return bool(visible) and sum(ch.isalpha() for ch in visible) * 5 >= len(visible) * 3


def looks_like_identifier(text: str) -> bool:
    """True for developer ids shown in place of a label (``card_item_0``, ``pkg:id/x``)."""
    t = str(text or "").strip()
    return bool(t) and not any(ch.isspace() for ch in t) and ("_" in t or ":id/" in t)


def find_selectable_group(
    elements: list[dict[str, Any]] | None,
    *,
    min_items: int = 2,
    width: int | None = None,
    height: int | None = None,
) -> tuple[bool | None, list[str]]:
    """A visible radio / checkbox group: ``(multi_select, labels)``.

    Structural only: accessibility nodes that are checkable (``checkable`` /
    ``checked`` attributes, or a CheckBox / RadioButton / Switch class) and
    sit in one column. ``multi_select`` is True for checkboxes, False for
    radio buttons and None when the widget class does not tell. A checkable
    node without its own label takes the text on its row. Returns
    ``(None, [])`` when no group of ``min_items`` labelled options is found.
    """
    if not elements:
        return None, []

    rights = [e["bounds"][2] for e in elements if isinstance(e, dict) and isinstance(e.get("bounds"), (list, tuple)) and len(e["bounds"]) == 4]
    bottoms = [e["bounds"][3] for e in elements if isinstance(e, dict) and isinstance(e.get("bounds"), (list, tuple)) and len(e["bounds"]) == 4]
    screen_w = int(width or 0) or (max(rights) if rights else 1)
    screen_h = int(height or 0) or (max(bottoms) if bottoms else 1)

    def _checkable(el: dict[str, Any]) -> bool:
        cls = str(el.get("class") or "")
        if any(k in cls for k in ("CheckBox", "RadioButton", "CheckedTextView")):
            return True
        if str(el.get("checkable") or "").strip().lower() == "true":
            return True
        # Custom-drawn tick boxes (no checkable flag): a small control with no
        # label of its own, or one whose developer id names a tick / radio widget.
        b = el.get("bounds")
        if not isinstance(b, (list, tuple)) or len(b) != 4:
            return False
        w, h = b[2] - b[0], b[3] - b[1]
        small = 0 < w <= screen_w * 0.15 and 0 < h <= screen_h * 0.08
        own = str(el.get("text") or "").strip()
        if not small:
            return False
        if not own:
            return True
        low = own.lower()
        return looks_like_identifier(own) and any(k in low for k in ("check", "radio", "tick"))

    def _row_label(el: dict[str, Any]) -> str:
        own = _clean_label(el.get("text"))
        if own and not looks_like_identifier(own):
            return own
        _, t0, _, b0 = el["bounds"]
        mid = (t0 + b0) / 2
        best, best_letters = "", 0
        for other in elements:
            ob = other.get("bounds") if isinstance(other, dict) else None
            if other is el or not isinstance(ob, (list, tuple)) or len(ob) != 4:
                continue
            txt = _clean_label(other.get("text"))
            if not txt or looks_like_identifier(txt) or not (ob[1] <= mid <= ob[3]):
                continue
            letters = sum(ch.isalpha() for ch in txt)
            if letters > best_letters:
                best, best_letters = txt, letters
        return best

    columns: dict[int, list[dict[str, Any]]] = {}
    for el in elements:
        if not isinstance(el, dict) or el.get("is_ocr") or not _checkable(el):
            continue
        b = el.get("bounds")
        if not isinstance(b, (list, tuple)) or len(b) != 4 or b[3] <= b[1]:
            continue
        cls = str(el.get("class") or "")
        if "Switch" in cls or "Toggle" in cls:
            continue  # settings toggles are independent on/off controls, not a choice group
        columns.setdefault(int(((b[0] + b[2]) / 2) // max(1, screen_w * 0.04)), []).append(el)

    best_labels: list[str] = []
    best_multi: bool | None = None
    for members in columns.values():
        labels: list[str] = []
        for m in sorted(members, key=lambda e: e["bounds"][1]):
            lab = _row_label(m)
            state = str(m.get("checked") or "").strip().lower()
            if lab and lab not in labels:
                labels.append(lab + (" (selected)" if state == "true" else ""))
        if len(labels) >= min_items and len(labels) > len(best_labels):
            classes = " ".join(str(m.get("class") or "") for m in members)
            if "RadioButton" in classes:
                best_multi = False
            elif "CheckBox" in classes:
                best_multi = True
            else:
                best_multi = None
            best_labels = labels
    return best_multi, best_labels


def selectable_group_notice(multi: bool | None, labels: list[str], *, max_examples: int = 4) -> str:
    """Per-turn hint shown when a radio / checkbox group is on screen."""
    kind_word = {True: "checkbox", False: "radio-button"}.get(multi, "selectable")
    examples = "; ".join(f"'{t[:50]}'" for t in labels[:max_examples])
    multi_hint = (
        "multi_select=true" if multi else "multi_select=false" if multi is False
        else "multi_select=true if several may be picked (checkboxes), false if only one (radio)"
    )
    return (
        f"A {kind_word} option group is visible ({len(labels)} options, e.g. {examples}). "
        "If the goal does not already say which of these to pick, do not pick for the user: "
        f"call ask_user_choice with kind='form_options', {multi_hint}, and list EVERY option "
        "of the group (scroll inside the form first if more options are hidden below), with "
        "any extra cost or 'already selected' as detail. A group marked required must be answered "
        "before the confirm button."
    )


def comparable_items_notice(texts: list[str], *, max_examples: int = 3) -> str:
    """Per-turn hint shown when several comparable items are on screen.

    Developer ids (cards without an accessibility label) are never quoted as
    examples: the model would copy them as option labels.
    """
    readable = [t for t in texts if not looks_like_identifier(t)]
    examples = (
        ", e.g. " + "; ".join(f"'{t[:60]}'" for t in readable[:max_examples]) if readable else ""
    )
    return (
        f"Several comparable items of the same kind are visible ({len(texts)}{examples}). "
        "This matters ONLY if these items are the candidates the goal asks to choose among "
        "(e.g. the results of the search the goal asked for); ignore it for unrelated feeds, "
        "banners or recommendations you pass on the way, and keep working toward the goal. "
        "If they are the candidates and the goal does not name the one to pick or give a rule "
        "that decides it (cheapest, first, best rated, a specific name or variant), do not choose "
        "for the user: call ask_user_choice with kind='pick_item'. For each option, read the item's real name "
        "and its distinguishing facts (price, rating, sold count…) from the screenshot: label = "
        "the name as shown, detail = those facts. Never use element ids or index numbers as labels."
    )


def user_answer_summary(answer: dict[str, Any]) -> str:
    """The user's answer in plain words, for the step history shown in the UI."""
    kind = str(answer.get("type") or "")
    if kind == "option":
        label = str(answer.get("label") or "")
        return f"{answer.get('option_id')}. {label}".strip(". ")
    if kind == "options":
        return ", ".join(str(x) for x in (answer.get("labels") or []))
    if kind == "custom":
        return f"Yêu cầu khác: {answer.get('text', '')}"
    if kind == "manual":
        return "Tự thao tác trên điện thoại rồi bấm Tiếp tục"
    if kind == "auto":
        return "Để AI tự chọn"
    return kind


def parse_choice_input(raw_input: str, pending: dict[str, Any]) -> dict[str, Any] | None:
    """Parse user terminal input into a ``choice_answer.json`` payload.

    Accepts a numbered option id (``1``..``N``), several ids for a checkbox
    group (``1,3`` or ``1 3``), ``a``/``auto``, ``m``/``manual``, or free-form
    custom instruction text. Returns ``None`` when input is empty.
    """
    raw = str(raw_input or "").strip()
    if not raw:
        return None
    choice_id = str(pending.get("choice_id") or "")
    lowered = raw.lower()
    valid_option_ids = {
        str(o.get("id"))
        for o in (pending.get("options") or [])
        if isinstance(o, dict) and "kind" not in o
    }
    if lowered in ("auto", "a"):
        return {"choice_id": choice_id, "type": "auto"}
    if lowered in ("manual", "m"):
        return {"choice_id": choice_id, "type": "manual"}
    if raw in valid_option_ids:
        return {"choice_id": choice_id, "type": "option", "option_id": raw}
    if pending.get("multi_select"):
        import re

        parts = [p for p in re.split(r"[\s,;]+", raw) if p]
        if len(parts) > 1 and all(p in valid_option_ids for p in parts):
            return {"choice_id": choice_id, "type": "options", "option_ids": list(dict.fromkeys(parts))}
    return {"choice_id": choice_id, "type": "custom", "text": raw}


def format_pending_choice_prompt(pending: dict[str, Any]) -> str:
    """Format a pending user choice as numbered lines for terminal/CLI display."""
    question = str(pending.get("question") or "").strip()
    reason = str(pending.get("reason") or "").strip()
    lines = [f"[?] {question}"]
    if reason:
        lines.append(f"    ({reason})")
    for opt in pending.get("options") or []:
        if not isinstance(opt, dict):
            continue
        oid = str(opt.get("id") or "")
        label = str(opt.get("label") or "")
        detail = str(opt.get("detail") or "").strip()
        shortcut = ""
        if oid == "auto":
            shortcut = " [a/auto]"
        elif oid == "manual":
            shortcut = " [làm xong trên điện thoại rồi gõ m/manual]"
        elif oid == "custom":
            shortcut = " [nhập chữ bất kỳ]"
        suffix = f" — {detail}" if detail else ""
        lines.append(f"  {oid}. {label}{shortcut}{suffix}")
    return "\n".join(lines)
