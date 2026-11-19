"""The written report of a Flash run, in the same place as Pro's (``notes/output.md``).

Flash always ends with ``report_task_status(explanation=...)``; the prompt asks
for that explanation in Markdown (tables when comparing). It is saved as the
``output`` note so every caller reads the result of either agent from one file.

When the caller describes the output it wants (``expected_output_desc`` /
``output_description``), the Outputter, the same agent Pro uses, rebuilds the
report from the full stored history. That matters on long runs: Flash's working
context is compressed as it goes, so details from early steps (the first of
four prices compared) may survive only in the stored steps the Outputter reads.
"""

from __future__ import annotations

import json
from typing import Any

from artemis.agents.outputter.outputter import OUTPUT_NOTE_KEY
from artemis.utils.notes import read_note_content, save_note_content
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


def _notes_base(ctx: Any):
    engine = getattr(ctx, "data_engine", None)
    base = getattr(engine, "base_dir", None) if engine else None
    return base


def render_flash_report(flash_result: dict, synthesized: Any = None) -> str:
    """The Markdown saved as ``output.md`` for a Flash run."""
    status = str(flash_result.get("status") or "unknown")
    if synthesized is not None:
        body = synthesized if isinstance(synthesized, str) else (
            "```json\n" + json.dumps(synthesized, ensure_ascii=False, indent=2) + "\n```"
        )
    else:
        body = str(flash_result.get("explanation") or "").strip() or "_(no explanation reported)_"
    return f"**Status:** {status}\n\n{body}\n"


def save_flash_report(ctx: Any, flash_result: dict, synthesized: Any = None) -> bool:
    """Writes the run's report to the ``output`` note unless one was already written.

    The Outputter may have saved its own report while it ran; that one is kept.
    Returns ``True`` when a report exists afterwards.
    """
    base = _notes_base(ctx)
    if not base:
        return False
    try:
        existing = read_note_content(base, OUTPUT_NOTE_KEY).strip()
    except Exception:  # pylint: disable=broad-exception-caught
        existing = ""
    if existing:
        return True
    try:
        save_note_content(base, OUTPUT_NOTE_KEY, render_flash_report(flash_result, synthesized))
        return True
    except Exception as err:  # pylint: disable=broad-exception-caught
        logger.warning(f"Could not save the Flash report: {err}")
        return False
