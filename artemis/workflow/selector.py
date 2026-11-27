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

"""Picks the recorded workflow for a new goal and fills its slots (KE_HOACH Mục 6.1, W2).

Fast-path (0 LLM calls): when the new goal matches a recorded goal verbatim or
differs ONLY in anchored slot spans (100% identical static framing around each
slot, separated by non-whitespace text).

Fallback: one text-only LLM call (optionally prioritized by live screen match).
Its answer is never trusted blindly: a slot value must literally appear in the
new goal, otherwise the slot stays unknown and the steps that need it go to the AI.
"""

from __future__ import annotations

import json
import re
from typing import Awaitable, Callable, Sequence, TypeVar

from artemis.workflow.fingerprint import ScreenFingerprint
from artemis.workflow.matcher import DEFAULT_DELTA, DEFAULT_TAU, is_matchable, match_screen
from artemis.workflow.store import Workflow, WorkflowIndexEntry

AskText = Callable[[str], Awaitable[str]]
WF = TypeVar("WF", Workflow, WorkflowIndexEntry)


def _escape_ws(text: str) -> str:
    if not text:
        return ""
    # Preserve leading/trailing whitespace sensitivity as \s* / \s+ and collapse internal whitespace.
    lead = r"\s+" if text[0].isspace() else ""
    trail = r"\s+" if len(text) > 1 and text[-1].isspace() else (r"\s+" if text[-1].isspace() and not lead else "")
    core = text.strip()
    if not core:
        return r"\s+"
    parts = re.split(r"\s+", core)
    return lead + r"\s+".join(re.escape(p) for p in parts) + trail


def match_goal_template(
    goal: str, wf_goal: str, wf_slots: dict[str, str]
) -> dict[str, str] | None:
    """Matches ``goal`` against ``wf_goal`` without an LLM when only slot values differ.

    Requires every slot value to sit between exact static text anchors (no two
    slots adjacent without non-whitespace separator). Returns ``{slot: value}``
    on match, else ``None``.
    """
    g_new = (goal or "").strip()
    g_old = (wf_goal or "").strip()
    if not g_new or not g_old:
        return None
    if g_old.casefold() == g_new.casefold():
        return dict(wf_slots)
    active_slots = {k: v.strip() for k, v in (wf_slots or {}).items() if v and v.strip()}
    if not active_slots:
        return None

    # Find non-overlapping occurrences of each slot value in g_old (longest values first).
    spans: list[tuple[int, int, str]] = []
    occupied = [False] * len(g_old)
    for name, val in sorted(active_slots.items(), key=lambda kv: len(kv[1]), reverse=True):
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            return None
        left_b = r"(?<!\w)" if val[0].isalnum() else ""
        right_b = r"(?!\w)" if val[-1].isalnum() else ""
        pat = left_b + re.escape(val) + right_b
        found_for_slot = 0
        for m in re.finditer(pat, g_old, re.IGNORECASE):
            s, e = m.start(), m.end()
            if any(occupied[s:e]):
                continue
            for idx in range(s, e):
                occupied[idx] = True
            spans.append((s, e, name))
            found_for_slot += 1
        if found_for_slot == 0:
            return None

    spans.sort(key=lambda item: item[0])
    # Require non-whitespace static separator between any two slots, and some static frame overall.
    for i in range(len(spans) - 1):
        between = g_old[spans[i][1] : spans[i + 1][0]]
        if not between.strip():
            return None

    regex_parts: list[str] = []
    static_chars = 0
    seen_slots: set[str] = set()
    cursor = 0
    for s, e, name in spans:
        seg = g_old[cursor:s]
        static_chars += len(seg.strip())
        regex_parts.append(_escape_ws(seg))
        if name not in seen_slots:
            seen_slots.add(name)
            regex_parts.append(f"(?P<{name}>.+?)")
        else:
            regex_parts.append(f"(?P={name})")
        cursor = e
    tail = g_old[cursor:]
    static_chars += len(tail.strip())
    regex_parts.append(_escape_ws(tail))

    if static_chars < 3:
        return None

    compiled = re.compile("^" + "".join(regex_parts) + "$", re.IGNORECASE | re.DOTALL)
    m = compiled.match(g_new)
    if not m:
        return None
    extracted: dict[str, str] = {}
    for name in active_slots:
        val = (m.group(name) or "").strip()
        if not val:
            return None
        extracted[name] = val
    return extracted


def _mask_slot_values_text(text: str, values: dict[str, str]) -> str:
    """Replaces literal slot values in ``text`` with a fixed marker and normalizes whitespace."""
    out = (text or "").strip().casefold()
    for val in sorted((v.strip() for v in (values or {}).values() if v and v.strip()), key=len, reverse=True):
        left_b = r"(?<!\w)" if val[0].isalnum() else ""
        right_b = r"(?!\w)" if val[-1].isalnum() else ""
        out = re.sub(left_b + re.escape(val.casefold()) + right_b, " <slot> ", out)
    return re.sub(r"\s+", " ", out).strip()


def goal_deviation_summary(
    goal: str,
    wf_goal: str,
    wf_slots: dict[str, str],
    matched_slots: dict[str, str] | None = None,
) -> str:
    """Describes extra or changed instructions between ``goal`` and ``wf_goal`` after masking slots (L-048).

    Returns ``""`` when ``goal`` follows the exact recorded static framing (only slot values differ).
    """
    if match_goal_template(goal, wf_goal, wf_slots) is not None:
        return ""
    masked_old = _mask_slot_values_text(wf_goal, wf_slots)
    slot_pool = dict(wf_slots or {})
    if matched_slots:
        slot_pool.update(matched_slots)
    masked_new = _mask_slot_values_text(goal, slot_pool)
    if not masked_new or masked_new == masked_old:
        return ""
    old_clauses = {
        re.sub(r"^[\W_]+|[\W_]+$", "", c).strip()
        for c in re.split(r"[.;,\n]+", masked_old)
        if c.strip()
    }
    extra_clauses: list[str] = []
    for raw_clause in re.split(r"[.;,\n]+", (goal or "").strip()):
        cleaned_raw = raw_clause.strip()
        if not cleaned_raw:
            continue
        masked_clause = re.sub(r"^[\W_]+|[\W_]+$", "", _mask_slot_values_text(cleaned_raw, slot_pool)).strip()
        if masked_clause and masked_clause not in old_clauses and masked_clause not in masked_old:
            extra_clauses.append(cleaned_raw)
    if extra_clauses:
        snippet = "; ".join(extra_clauses)
        if len(snippet) > 180:
            snippet = snippet[:177] + "..."
        return f"task differs from recorded template ({wf_goal!r}); new/changed instruction: {snippet!r}"
    return f"task wording differs from recorded template ({wf_goal!r})"


def select_workflow_fast(
    goal: str, candidates: Sequence[WF]
) -> tuple[WF | None, dict[str, str]]:
    """Deterministic zero-LLM selection across ``candidates``."""
    if not candidates:
        return None, {}
    for wf in candidates:
        if wf.goal.strip().casefold() == goal.strip().casefold():
            return wf, dict(wf.slots)
    matches: list[tuple[WF, dict[str, str]]] = []
    for wf in candidates:
        extracted = match_goal_template(goal, wf.goal, wf.slots)
        if extracted is not None:
            matches.append((wf, extracted))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Prefer the candidate with more successful runs if clearly ahead.
        ranked = sorted(matches, key=lambda item: (-getattr(item[0], "ok_runs", 0), getattr(item[0], "workflow_id", "")))
        if getattr(ranked[0][0], "ok_runs", 0) > getattr(ranked[1][0], "ok_runs", 0):
            return ranked[0]
    return None, {}


def filter_by_live_screen(
    candidates: Sequence[Workflow],
    live_fp: ScreenFingerprint | None,
    *,
    tau: float = DEFAULT_TAU,
    delta: float = DEFAULT_DELTA,
) -> list[Workflow]:
    """Prioritizes workflows having a node matching ``live_fp``; falls back to all if none match."""
    cands = list(candidates)
    if not cands or live_fp is None or not is_matchable(live_fp):
        return cands
    matched: list[Workflow] = []
    for wf in cands:
        fps = wf.node_fps()
        if fps and match_screen(live_fp, fps, tau=tau, delta=delta, min_app_tokens=1).matched:
            matched.append(wf)
    return matched if matched else cands


def build_prompt(goal: str, candidates: Sequence[Workflow | WorkflowIndexEntry]) -> str:
    lines = [
        "You map a new phone task onto a workflow recorded earlier in the same app.",
        "Recorded workflows:",
    ]
    for i, wf in enumerate(candidates, 1):
        lines.append(f"{i}) task: {json.dumps(wf.goal, ensure_ascii=False)}; slots: {json.dumps(wf.slots, ensure_ascii=False)}")
    lines += [
        f"New task: {json.dumps(goal, ensure_ascii=False)}",
        "Pick a workflow only if the new task is the same kind of task (same steps),"
        " differing at most in the slot values. For each slot give the value for the"
        " new task copied exactly from the new task text, or null if it has none.",
        'Answer with JSON only: {"workflow": <number or null>, "slots": {"slot1": "..."}}',
    ]
    return "\n".join(lines)


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def select_workflow(
    goal: str,
    candidates: Sequence[WF],
    ask: AskText | None,
    *,
    live_fp: ScreenFingerprint | None = None,
) -> tuple[WF | None, dict[str, str]]:
    if not candidates:
        return None, {}
    wf_fast, vals_fast = select_workflow_fast(goal, candidates)
    if wf_fast is not None:
        return wf_fast, vals_fast
    if ask is None:
        return None, {}
    pool: Sequence[WF] = candidates
    if live_fp is not None and all(isinstance(c, Workflow) for c in candidates):
        pool = filter_by_live_screen(candidates, live_fp)  # type: ignore[arg-type]
    try:
        data = _parse(await ask(build_prompt(goal, pool)))
    except Exception:
        return None, {}
    idx = data.get("workflow")
    if not isinstance(idx, int) or not 1 <= idx <= len(pool):
        return None, {}
    wf = pool[idx - 1]
    g = goal.casefold()
    values: dict[str, str] = {}
    raw = data.get("slots") if isinstance(data.get("slots"), dict) else {}
    for name in wf.slots:
        v = raw.get(name)
        if isinstance(v, str) and v.strip() and v.strip().casefold() in g:
            values[name] = v.strip()
    return wf, values
