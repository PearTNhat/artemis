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

"""Deterministic natural-language playbook generation from a Workflow graph (W1)."""

from __future__ import annotations

from collections import deque
import re

from artemis.workflow.recorder import INPUT_TOOLS
from artemis.workflow.store import Edge, Workflow


def _substitute_slots(text: str, slot_values: dict[str, str]) -> str:
    out = str(text or "").strip()
    if not out:
        return ""
    for name, val in slot_values.items():
        if val:
            out = out.replace(f"{{{name}}}", val)
    return re.sub(r"\s+", " ", out).strip()


def _best_path_to_end(wf: Workflow, start: str, active: list[Edge]) -> dict[str, Edge]:
    """Returns next-edge map along a highest-ok simple path from ``start`` to ``end_nodes``."""
    end_set = set(wf.end_nodes)
    if not end_set:
        return {}
    by_src: dict[str, list[Edge]] = {}
    for e in active:
        if e.dst is not None and e.dst != e.src:
            by_src.setdefault(e.src, []).append(e)

    # Shortest hop BFS from start to any end_node, breaking ties by active + higher edge.ok.
    q: deque[tuple[str, list[Edge]]] = deque([(start, [])])
    seen: set[str] = {start}
    while q:
        node, path = q.popleft()
        if node in end_set and path:
            return {e.src: e for e in path}
        cands = sorted(by_src.get(node, []), key=lambda e: (-int(e.state == "active"), -e.ok, wf.edges.index(e)))
        for e in cands:
            assert e.dst is not None
            if e.dst in end_set:
                full = path + [e]
                return {step.src: step for step in full}
            if e.dst not in seen:
                seen.add(e.dst)
                q.append((e.dst, path + [e]))
    return {}


def main_path(wf: Workflow) -> list[Edge]:
    """Selects ONE clean main execution path through the Workflow graph.

    Excludes disabled edges, prefers active edges and higher ``stats.ok``, keeps
    in-place self-loop steps (e.g. typing multiple form fields on one screen),
    and traverses disconnected segments after ``judgement`` steps in recorded
    order without looping.
    """
    active = [e for e in wf.edges if e.state != "disabled"]
    if not active:
        return []
    start = wf.start_nodes[0] if wf.start_nodes else active[0].src
    end_set = set(wf.end_nodes)
    order = {e.id: idx for idx, e in enumerate(wf.edges)}
    to_end = _best_path_to_end(wf, start, active)

    by_src: dict[str, list[Edge]] = {}
    for e in active:
        by_src.setdefault(e.src, []).append(e)

    path: list[Edge] = []
    used_edges: set[str] = set()
    visited_nodes: set[str] = set()
    cur: str | None = start

    while len(used_edges) < len(active):
        if cur is None:
            nxt = next(
                (e.src for e in active if e.id not in used_edges and e.src not in visited_nodes),
                None,
            )
            if nxt is None:
                break
            cur = nxt
            to_end = _best_path_to_end(wf, cur, active)

        visited_nodes.add(cur)
        edges_here = [e for e in by_src.get(cur, []) if e.id not in used_edges]

        # Include in-place steps (e.g., multiple fields on a form) in recorded order.
        in_place = [
            e
            for e in edges_here
            if e.dst == cur and e.kind in ("nav", "slot")
        ]
        for e in sorted(in_place, key=lambda x: order[x.id]):
            path.append(e)
            used_edges.add(e.id)

        # Pick ONE outgoing step from cur.
        outgoing = [
            e
            for e in edges_here
            if e.id not in used_edges and e.dst != cur
        ]
        if not outgoing:
            cur = None
            continue

        chosen = to_end.get(cur)
        if chosen is None or chosen.id in used_edges:
            unvisited_out = [
                e for e in outgoing if e.dst is None or e.dst not in visited_nodes or e.dst in end_set
            ]
            pool = unvisited_out or outgoing
            chosen = min(
                pool,
                key=lambda e: (
                    0 if e.kind in ("nav", "slot") else 1,
                    -e.ok,
                    order[e.id],
                ),
            )

        path.append(chosen)
        used_edges.add(chosen.id)
        if chosen.dst is None or chosen.dst in end_set or chosen.dst in visited_nodes:
            if chosen.dst in end_set and len(path) >= 1:
                break
            cur = None
        else:
            cur = chosen.dst

    return path


def format_step(edge: Edge, slot_values: dict[str, str]) -> str:
    """Formats a single edge into a concise human/model instruction."""
    val = slot_values.get(edge.slot, "") if edge.slot else ""
    label = _substitute_slots(edge.label, slot_values)
    desc = _substitute_slots(edge.description, slot_values)
    t_text = edge.target.text if edge.target else ""
    t_hint = edge.target.hint if edge.target else ""

    if edge.tool == "press_key" and edge.key:
        return f"press {edge.key}"
    if edge.tool in INPUT_TOOLS:
        field_name = t_hint or desc or label or "input field"
        shown_val = f"'{val}'" if val else f"<{edge.slot or 'value'}>"
        return f"type {shown_val} into '{field_name}'"
    if edge.kind == "slot":
        shown_val = f"'{val}'" if val else f"<{edge.slot}>"
        if desc and desc.casefold() != val.casefold():
            return f"tap item matching {shown_val} ({desc})"
        return f"tap item matching {shown_val}"
    if edge.kind == "judgement":
        detail = desc or label or "matching item on screen"
        return f"choose on screen: {detail}"
    if edge.kind == "commitment_stop":
        detail = label or desc or "final confirmation control"
        return f"verify everything carefully before '{detail}'"
    target_name = label or t_text or desc or (edge.target.resource_id if edge.target else "") or "control"
    if desc and desc.casefold() != target_name.casefold() and len(desc) <= 60:
        return f"tap '{target_name}' ({desc})"
    return f"tap '{target_name}'"


def build_playbook(
    wf: Workflow,
    slot_values: dict[str, str],
    *,
    done_edge_ids: set[str] | None = None,
    show_progress: bool = False,
) -> str:
    """Builds a deterministic playbook from ``main_path(wf)`` with optional step markers."""
    steps = main_path(wf)
    if not steps:
        return ""
    done = done_edge_ids or set()
    marked_next = False
    parts: list[str] = []
    for idx, edge in enumerate(steps, 1):
        text = format_step(edge, slot_values)
        if show_progress and done:
            if edge.id in done:
                parts.append(f"{idx}) [done] {text}")
            elif not marked_next:
                parts.append(f"{idx}) [NEXT] {text}")
                marked_next = True
            else:
                parts.append(f"{idx}) {text}")
        else:
            parts.append(f"{idx}) {text}")
    end_idx = len(steps) + 1
    parts.append(
        f"{end_idx}) on the final screen, verify the goal (scroll if a list may hold the added item) and call report_task_status"
    )
    prefix = (
        "[Playbook] Guide from an earlier successful run (adapt to the live screen if it differs): "
        if not (show_progress and done)
        else "[Playbook] Progress on recorded guide: "
    )
    return prefix + "; ".join(parts) + "."


def next_playbook_step(
    wf: Workflow,
    slot_values: dict[str, str],
    done_edge_ids: set[str],
) -> str:
    """Returns the next pending playbook step description, or empty string if at end."""
    for edge in main_path(wf):
        if edge.id not in done_edge_ids:
            return format_step(edge, slot_values)
    return ""

