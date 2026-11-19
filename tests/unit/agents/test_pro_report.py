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

from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from artemis.agents.outputter.pro_report import render_pro_report, save_pro_report
from artemis.graph.checkpoints import append_ledger_record
from artemis.utils.notes import read_note_content, save_note_content


def test_save_pro_report_preserves_existing_output(tmp_path: Path):
    ctx = SimpleNamespace(data_engine=SimpleNamespace(base_dir=tmp_path))
    save_note_content(tmp_path, "output", "# Existing Report\nAlready generated.")

    saved = save_pro_report(ctx, last_state=None, run_outcome={"task_status": "completed"})
    assert saved is True
    assert read_note_content(tmp_path, "output").strip() == "# Existing Report\nAlready generated."


def test_save_pro_report_generates_markdown_from_plan_and_ledger(tmp_path: Path):
    ctx = SimpleNamespace(data_engine=SimpleNamespace(base_dir=tmp_path))

    # 1. Create a task plan
    plan_text = "- [x] Open Shopee and search for cable\n  - verify: Results shown\n- [x] Select cheapest item\n"
    save_note_content(tmp_path, "task_plan", plan_text)

    # 2. Create ledger entries
    append_ledger_record(
        tmp_path,
        {
            "checkpoint_id": "cp_1",
            "item_text": "Results shown",
            "status": "passed",
            "evidence": "Found 20 items in grid",
        },
    )
    append_ledger_record(
        tmp_path,
        {
            "checkpoint_id": "cp_2",
            "item_text": "Final checkout screen displayed",
            "status": "passed",
            "evidence": "Visible Order button at bottom",
        },
    )

    # 3. Create another note
    save_note_content(tmp_path, "prices", "Product A: 11.900đ\nProduct B: 18.000đ")

    last_state = SimpleNamespace(initial_goal="Order cheap cable on Shopee", plan=plan_text)
    run_outcome = {"task_status": "completed", "goal": "Order cheap cable on Shopee"}

    saved = save_pro_report(ctx, last_state=last_state, run_outcome=run_outcome)
    assert saved is True

    content = read_note_content(tmp_path, "output")
    assert "# Task Report: Order cheap cable on Shopee" in content
    assert "**Status:** completed" in content
    assert "## Execution Plan & Milestones" in content
    assert "- [x] Open Shopee and search for cable" in content
    assert "## Verification Verdicts" in content
    assert "✅ **[PASSED]** `Results shown`: Found 20 items in grid" in content
    assert "## Findings & Notes" in content
    assert "### Note: prices" in content
    assert "Product A: 11.900đ" in content


def test_save_pro_report_blocked_status(tmp_path: Path):
    ctx = SimpleNamespace(data_engine=SimpleNamespace(base_dir=tmp_path))
    last_state = SimpleNamespace(initial_goal="Buy concert ticket")
    run_outcome = {"task_status": "blocked", "goal": "Buy concert ticket"}

    saved = save_pro_report(ctx, last_state=last_state, run_outcome=run_outcome)
    assert saved is True

    content = read_note_content(tmp_path, "output")
    assert "**Status:** failed" in content
