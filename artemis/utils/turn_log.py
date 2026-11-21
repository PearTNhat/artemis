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

"""One JSON line per Flash turn, for bottleneck and mis-tap analysis.

Enabled when AI I/O debug is active (via ``ARTEMIS_DEBUG_AI_IO=true`` or the
Web UI toggle). Written to ``<session trace dir>/turns.jsonl``. Each line holds
where the turn's time went (model, actions, post-action capture, prompt prep), the
model's token counts, the payload shape (element-list size, image setting),
and every tool call with its resolved tap point and outcome. No prompt text,
no screenshot: it stays small (~0.5 KB/turn) and free of screen content beyond
the short outcome messages.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from artemis.utils.ai_io_logger import is_ai_io_debug_enabled

TURN_LOG_FILENAME = "turns.jsonl"
_MAX_TEXT = 160


def turn_log_enabled() -> bool:
    """True when AI I/O debug is enabled (via ARTEMIS_DEBUG_AI_IO or Web UI)."""
    return is_ai_io_debug_enabled()


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_TEXT:
        return value[:_MAX_TEXT] + "…"
    return value


def compact_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Tool arguments with long strings cut (typed text, explanations)."""
    out: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if isinstance(value, list):
            out[key] = [_short(v) if not isinstance(v, dict) else
                        {k: _short(x) for k, x in v.items()} for v in value]
        elif isinstance(value, dict):
            out[key] = {k: _short(x) for k, x in value.items()}
        else:
            out[key] = _short(value)
    return out


def usage_fields(response: Any) -> dict[str, Any]:
    """Provider token counts from a LangChain reply (``usage_metadata``)."""
    usage = getattr(response, "usage_metadata", None) or {}
    if not isinstance(usage, dict):
        return {}
    fields = {
        "in_tok": usage.get("input_tokens"),
        "cached_tok": (usage.get("input_token_details") or {}).get("cache_read"),
        "out_tok": usage.get("output_tokens"),
        "reasoning_tok": (usage.get("output_token_details") or {}).get("reasoning"),
    }
    return {k: v for k, v in fields.items() if v is not None}


def action_entry(name: str, args: dict[str, Any] | None, exec_result: Any) -> dict[str, Any]:
    """One tool call: what was asked, where it landed, how it ended."""
    meta = getattr(exec_result, "metadata", None) or {}
    entry: dict[str, Any] = {
        "tool": name,
        "args": compact_args(args),
        "status": getattr(exec_result, "status", None),
    }
    for src, dst in (
        ("code", "code"),
        ("target_coordinates", "tap_0_1000"),
        ("no_effect", "no_effect"),
        ("timed_out", "timed_out"),
        ("action_s", "action_s"),
        ("observe_s", "observe_s"),
    ):
        value = meta.get(src)
        if value not in (None, False):
            entry[dst] = value
    detail = meta.get("observe_detail")
    if detail:
        entry["obs"] = detail
    message = getattr(exec_result, "text_summary", None)
    if message:
        entry["msg"] = _short(message)
    return entry


def append_turn(directory: Path | str | None, record: dict[str, Any]) -> Path | None:
    """Appends one record; never raises (logging must not break a run)."""
    if directory is None:
        return None
    try:
        path = Path(directory) / TURN_LOG_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return path
    except Exception:  # pylint: disable=broad-exception-caught
        return None
