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

"""The written report of a Pro run, saved to notes/output.md."""

from __future__ import annotations

import json
from typing import Any

from artemis.agents.outputter.outputter import OUTPUT_NOTE_KEY
from artemis.graph.checkpoints import read_ledger
from artemis.utils.notes import list_notes_info, read_note_content, save_note_content
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


def _notes_base(ctx: Any):
    engine = getattr(ctx, "data_engine", None)
    base = getattr(engine, "base_dir", None) if engine else None
    return base


def render_pro_report(
    ctx: Any,
    last_state: Any = None,
    run_outcome: dict | None = None,
    output: Any = None,
) -> str:
    """The Markdown saved as output.md for a Pro run when Outputter did not run."""
    base = _notes_base(ctx)
    lines: list[str] = []

    goal = (
        getattr(last_state, "initial_goal", None)
        or (run_outcome or {}).get("goal")
        or "Mobile Automation Task"
    )
    task_status = (run_outcome or {}).get("task_status") or "completed"
    status_label = "failed" if task_status == "blocked" else "completed"

    lines.append(f"# Task Report: {goal}\n")
    lines.append(f"**Status:** {status_label}\n")

    # If structured output exists
    if output:
        if isinstance(output, str):
            lines.append(f"## Summary\n{output.strip()}\n")
        elif isinstance(output, dict):
            lines.append(
                "## Structured Output\n```json\n"
                + json.dumps(output, ensure_ascii=False, indent=2)
                + "\n```\n"
            )

    # Plan & Milestones from task_plan.md
    task_plan_content = ""
    if base:
        try:
            task_plan_content = read_note_content(base, "task_plan").strip()
        except Exception:
            pass
    if not task_plan_content and last_state:
        task_plan_content = getattr(last_state, "plan", "") or ""

    if task_plan_content:
        lines.append("## Execution Plan & Milestones\n")
        lines.append(task_plan_content)
        lines.append("")

    # Check / Verification Verdicts
    records = read_ledger(base) if base else []
    if records:
        lines.append("## Verification Verdicts\n")
        for rec in records:
            item = rec.get("item_text") or rec.get("check_kind") or "Check item"
            st = str(rec.get("status") or "unknown").lower()
            ev = rec.get("evidence") or ""
            icon = "✅" if st == "passed" else ("❌" if st == "failed" else "⚠️")
            lines.append(f"- {icon} **[{st.upper()}]** `{item}`: {ev}")
        lines.append("")

    # Operator Notes & Discoveries (excluding task_plan and output)
    if base:
        try:
            notes_info = list_notes_info(base)
            other_keys = [
                k for k in notes_info.keys() if k not in ("task_plan", "output", "plan")
            ]
            if other_keys:
                lines.append("## Findings & Notes\n")
                for key in other_keys:
                    content = read_note_content(base, key).strip()
                    if content:
                        lines.append(f"### Note: {key}\n{content}\n")
        except Exception:
            pass

    return "\n".join(lines).strip() + "\n"


def save_pro_report(
    ctx: Any,
    last_state: Any = None,
    run_outcome: dict | None = None,
    output: Any = None,
) -> bool:
    """Writes the Pro run's report to the output note unless one was already written."""
    base = _notes_base(ctx)
    if not base:
        return False
    try:
        existing = read_note_content(base, OUTPUT_NOTE_KEY).strip()
    except Exception:
        existing = ""
    if existing:
        return True
    try:
        report_md = render_pro_report(ctx, last_state, run_outcome, output)
        save_note_content(base, OUTPUT_NOTE_KEY, report_md)
        return True
    except Exception as err:
        logger.warning(f"Could not save the Pro report: {err}")
        return False
