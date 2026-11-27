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

"""Builds / merges a workflow from one successful Flash run (KE_HOACH Mục 3.2, 3.5).

Input is the in-memory list of device actions the run executed, each with the
raw UI tree before and after it. Classification is deterministic and
app-agnostic (no LLM call, no wording):

- typed text that appears in the goal -> ``slot`` (value differs per task);
- tap on an element whose label contains a slot value -> ``slot`` pick;
- tap on a list item (inside a scrolling container), any tap right after the
  user answered ``ask_user_choice``, typed text not in the goal -> ``judgement``;
- tap on a commitment control -> ``commitment_stop``;
- other taps, ENTER / SEARCH / BACK -> ``nav``.

A runnable edge (nav / slot) is only kept when its destination screen is known,
so the autopilot can verify every step it replays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from artemis.workflow.fingerprint import ScreenFingerprint, app_fingerprint, normalize
from artemis.workflow.matcher import DEFAULT_DELTA, DEFAULT_TAU, is_matchable, match_screen, weighted_jaccard
from artemis.workflow.store import Edge, Node, Workflow
from artemis.workflow.targets import (
    TargetDescriptor,
    anchors_for,
    descriptor_for,
    fold,
    resolve,
    is_commitment_at,
    is_editable,
    is_list_item,
    node_at,
    node_label,
    typed_slots,
    visible_app_nodes,
)

# Layout similarity above which the screens before and after a swipe are the
# same screen scrolled (a long form), not a new screen.
SAME_SCREEN_SCROLLED = 0.5
TAP_TOOLS = frozenset({"click", "tap", "long_press"})
INPUT_TOOLS = frozenset({"input_text", "focus_and_input_text"})
NAV_KEYS = frozenset({"ENTER", "66", "SEARCH", "84", "BACK", "4"})


@dataclass
class RawScreen:
    elements: Any
    width: int | None = None
    height: int | None = None
    seq: int | None = None


@dataclass
class RecordedStep:
    tool: str
    args: dict[str, Any]
    action: dict[str, Any] = field(default_factory=dict)  # runner action record
    ok: bool = True
    no_effect: bool = False
    pre: RawScreen | None = None
    post: RawScreen | None = None
    after_user_choice: bool = False
    autopilot: bool = False


@dataclass
class _Screen:
    norm: list[dict[str, Any]]
    fp: ScreenFingerprint


def _screen(raw: RawScreen | None) -> _Screen | None:
    if raw is None or not raw.elements:
        return None
    norm = normalize(raw.elements, width=raw.width, height=raw.height)
    fp = app_fingerprint(norm)
    if not is_matchable(fp):
        return None
    return _Screen(norm, fp)


def detect_slots(steps: list[RecordedStep], goal: str) -> dict[str, str]:
    """Typed values that also appear in the goal become slots (slot1, slot2...)."""
    slots: dict[str, str] = {}
    g = (goal or "").casefold()
    for st in steps:
        if st.tool in INPUT_TOOLS and st.ok:
            text = str(st.args.get("text") or "").strip()
            if text and text.casefold() in g and text not in slots.values():
                slots[f"slot{len(slots) + 1}"] = text
    return slots


def _slot_for_text(text: str, slot_values: dict[str, str]) -> str:
    t = (text or "").strip().casefold()
    for name, value in slot_values.items():
        if value and value.strip().casefold() == t:
            return name
    return ""


def _slot_in_label(label: str, slot_values: dict[str, str]) -> str:
    lab = fold(label)
    for name, value in slot_values.items():
        if value and fold(value) in lab:
            return name
    return ""


def _node_for(wf: Workflow, fp: ScreenFingerprint, tau: float, delta: float) -> str:
    """Existing node the screen matches (same criteria as replay), else a new one."""
    fps = wf.node_fps()
    if fps:
        m = match_screen(fp, fps, tau=tau, delta=delta, min_app_tokens=1)
        if m.matched and m.node_id:
            wf.nodes[m.node_id].add_fp(fp)
            return m.node_id
    nid = wf.new_node_id()
    wf.nodes[nid] = Node(nid, [fp])
    return nid


def _point(action: dict[str, Any]) -> tuple[int, int] | None:
    pt = action.get("normalized_coordinates")
    if isinstance(pt, (list, tuple)) and len(pt) == 2:
        try:
            return int(pt[0]), int(pt[1])
        except (TypeError, ValueError):
            return None
    return None


def _box(action: dict[str, Any]) -> tuple[int, int, int, int] | None:
    b = action.get("target_bounds")
    if isinstance(b, (list, tuple)) and len(b) == 4:
        try:
            return int(b[0]), int(b[1]), int(b[2]), int(b[3])
        except (TypeError, ValueError):
            return None
    return None


def _classify(
    st: RecordedStep, pre: _Screen, slot_values: dict[str, str]
) -> tuple[str, str, TargetDescriptor | None, str]:
    """-> (kind, slot, target, key). kind '' means: not an edge."""
    tool = st.tool
    if tool in TAP_TOOLS:
        node = node_at(
            pre.norm,
            point=_point(st.action),
            box=_box(st.action),
            resource_id=str(st.action.get("target_resource_id") or ""),
        )
        if node is None:
            return "judgement", "", None, ""
        if is_editable(node):
            # A tap on a field: if it only focuses it (same screen after), the
            # input step that follows carries its own target, so it is not a
            # step; if it opens another screen (a search bar) it is navigation.
            return "focus", "", descriptor_for(node), ""
        label = node_label(node)
        nb = tuple(node["bounds"])
        if is_commitment_at(pre.norm, nb, extra_texts=(label, str(st.action.get("target_text") or ""))):  # type: ignore[arg-type]
            return "commitment_stop", "", None, ""
        if st.after_user_choice:
            return "judgement", "", None, ""
        slot = _slot_in_label(label, slot_values)
        if slot:
            return "slot", slot, descriptor_for(node, slot=slot), ""
        if is_list_item(node, pre.norm):
            return "judgement", "", None, ""
        desc = descriptor_for(node)
        if not (desc.resource_id or desc.text or desc.class_name):
            return "judgement", "", None, ""
        return "nav", "", desc, ""
    if tool in INPUT_TOOLS:
        slot = _slot_for_text(str(st.args.get("text") or ""), slot_values)
        if not slot:
            return "judgement", "", None, ""
        # Type into the field itself, never into a hint label laid over it.
        fields = [n for n in visible_app_nodes(pre.norm) if is_editable(n)]
        node = node_at(pre.norm, point=_point(st.action), box=_box(st.action))
        if node is None or not is_editable(node):
            node = fields[0] if len(fields) == 1 else None
        target = descriptor_for(node) if node is not None else TargetDescriptor(focused_editable=True)
        if target.text:
            target.text = ""  # a field's text is its old content, not a label
        return "slot", slot, target, ""
    if tool == "press_key":
        key = str(st.args.get("key") or st.args.get("keycode") or "").strip().upper()
        if key in NAV_KEYS:
            return "nav", "", None, key
        return "judgement", "", None, ""
    # waits, swipes, click_sequence, manage_app...: no edge. A scroll does not
    # make the screen a decision point; the tap that follows it is recorded on
    # its own pre-screen (and fails to resolve on replay when off-screen).
    return "", "", None, ""


def _value_persisted(i: int, screens: list, value: str) -> bool:
    """Is the value typed at step ``i`` still in that field on the last screen
    of the run where the field can be found?"""
    st, pre, _ = screens[i]
    node = node_at(pre.norm, point=_point(st.action), box=_box(st.action))
    if node is None or not is_editable(node):
        return False
    desc = descriptor_for(node)
    desc.text = ""
    for _, later_pre, later_post in reversed(screens[i:]):
        for scr in (later_post, later_pre):
            if scr is None:
                continue
            field, status = resolve(desc, scr.norm)
            if field is not None or status != "not_found":
                return field is not None and fold(value) in fold(str(field.get("text") or ""))
    return False


def record_run(
    steps: list[RecordedStep],
    *,
    goal: str,
    workflow: Workflow | None = None,
    workflow_id: str = "",
    slot_values: dict[str, str] | None = None,
    tau: float = DEFAULT_TAU,
    delta: float = DEFAULT_DELTA,
) -> Workflow | None:
    """Builds a new workflow (``workflow`` None) or merges into ``workflow``.

    ``slot_values`` are this run's values per slot name (for a merge: the
    values the selector extracted from the new goal). Returns None when the
    run holds nothing replayable.
    """
    screens = [(st, _screen(st.pre), _screen(st.post)) for st in steps]
    pkgs: dict[str, int] = {}
    for _, pre, _ in screens:
        if pre is not None:
            pkgs[pre.fp.package] = pkgs.get(pre.fp.package, 0) + 1
    if workflow is None:
        if not pkgs:
            return None
        package = max(pkgs, key=lambda p: pkgs[p])
        slots = dict(slot_values or detect_slots(steps, goal))
        wf = Workflow(workflow_id=workflow_id or f"{package}__1", package=package, goal=goal, slots=slots)
        values = slots
    else:
        wf = workflow
        values = dict(slot_values or {})
    package = wf.package

    # Drop an action the AI undid right away (next action BACK landing on the
    # screen it started from).
    skip: set[int] = set()
    for i in range(len(screens) - 1):
        st, pre, _ = screens[i]
        nxt, _, nxt_post = screens[i + 1]
        if (
            pre is not None
            and nxt_post is not None
            and nxt.tool == "press_key"
            and str(nxt.args.get("key") or "").upper() in ("BACK", "4")
        ):
            if match_screen(nxt_post.fp, {"pre": [pre.fp]}, tau=tau, delta=0.0, min_app_tokens=1).matched:
                skip.update({i, i + 1})

    # A slot typed more than once (a mistake corrected, a wrong field fixed):
    # keep the typing whose value is still in its field on the last screen
    # that shows that field (what was actually submitted); unknown -> the last.
    typings: dict[str, list[int]] = {}
    for i, (st, pre, _) in enumerate(screens):
        if st.tool in INPUT_TOOLS and st.ok and not st.autopilot and pre is not None:
            slot = _slot_for_text(str(st.args.get("text") or ""), values)
            if slot:
                typings.setdefault(slot, []).append(i)
    for slot, idxs in typings.items():
        if len(idxs) < 2:
            continue
        persisted = [i for i in idxs if _value_persisted(i, screens, values[slot])]
        keep = persisted[-1] if persisted else idxs[-1]
        skip.update(i for i in idxs if i != keep)

    known = {e.signature() for e in wf.edges}
    added = 0
    for i, (st, pre, post) in enumerate(screens):
        if (
            st.tool in ("swipe",) + tuple(INPUT_TOOLS)
            and st.ok
            and pre is not None
            and post is not None
            and pre.fp.package == post.fp.package == package
            and weighted_jaccard(pre.fp, post.fp) >= SAME_SCREEN_SCROLLED
        ):
            # Scrolling inside a screen (a long form) shows other parts of the
            # same screen (also when typing opened the keyboard): one node, so steps recorded after the scroll apply
            # from the top too (the autopilot scrolls to off-screen targets).
            # Typing too: the keyboard opening auto-scrolls a form.
            nid = _node_for(wf, pre.fp, tau, delta)
            if not match_screen(post.fp, {nid: wf.nodes[nid].fps}, tau=tau, delta=0.0, min_app_tokens=1).matched:
                wf.nodes[nid].add_fp(post.fp)
            if st.tool == "swipe":
                continue
        if i in skip or st.autopilot or not st.ok or st.no_effect:
            continue
        if pre is None or pre.fp.package != package:
            continue
        kind, slot, target, key = _classify(st, pre, values)
        if not kind:
            continue
        src = _node_for(wf, pre.fp, tau, delta)
        dst = None
        anchors: list[dict[str, str]] = []
        if post is not None and post.fp.package == package:
            dst = _node_for(wf, post.fp, tau, delta)
            anchors = anchors_for(post.norm, pre.norm, exclude_texts=values.values())
        if kind == "focus":
            if dst is None or dst == src:
                continue
            kind = "nav"
        if st.tool in INPUT_TOOLS:
            anchors = []  # verified by the typed value itself (autopilot)
        if kind in ("nav", "slot") and dst == src and st.tool not in INPUT_TOOLS:
            # Same screen layout after the tap (fields expanded, keyboard
            # hidden): not a step and not a choice. Replay just goes on; if the
            # next target then is missing, the autopilot hands over.
            continue
        if kind in ("nav", "slot") and dst is None:
            # Unverifiable (left the app, tree too poor).
            kind, slot, target, key = "judgement", "", None, ""
        typed = ""
        if kind in ("nav", "slot") and st.tool not in INPUT_TOOLS:
            # Every value already in the form when this step ran: a submit
            # recorded after all fields were filled waits for all of them.
            typed = ",".join(sorted(typed_slots(pre.norm, values)))
        edge = Edge(
            id="",
            typed=typed,
            src=src,
            dst=dst if kind in ("nav", "slot") else None,
            kind=kind,
            tool=st.tool if kind != "commitment_stop" else "",
            key=key,
            slot=slot,
            target=target,
            anchors=anchors if kind in ("nav", "slot") else [],
        )
        sig = edge.signature()
        if sig in known:
            continue
        known.add(sig)
        edge.id = wf.new_edge_id()
        wf.edges.append(edge)
        added += 1

    if workflow is None and not any(e.kind in ("nav", "slot") for e in wf.edges):
        return None
    # The screen the run was reported completed on (after its last action).
    for st, pre, post in reversed(screens):
        last = post if post is not None else pre
        if last is not None and last.fp.package == package:
            end = _node_for(wf, last.fp, tau, delta)
            if end not in wf.end_nodes:
                wf.end_nodes.append(end)
            break
    return wf
