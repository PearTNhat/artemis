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
import re
from typing import Any, Iterable

from artemis.workflow.fingerprint import ScreenFingerprint, app_fingerprint, foreground_package, normalize
from artemis.workflow.matcher import DEFAULT_DELTA, DEFAULT_TAU, is_matchable, match_screen, weighted_jaccard
from artemis.workflow.store import Edge, Node, Workflow
from third_party.mobile_use.utils.logger import get_logger
from artemis.workflow.targets import (
    is_data_label,
    TargetDescriptor,
    anchors_for,
    descriptor_for,
    editable_field_keys,
    field_has_value,
    field_target_matches,
    fold,
    resolve,
    is_commitment_at,
    is_editable,
    is_list_item,
    keyboard_box,
    node_at,
    node_label,
    screen_content_key,
    typed_slots,
    visible_app_nodes,
)

# Layout similarity above which the screens before and after a swipe are the
# same screen scrolled (a long form), not a new screen.
SAME_SCREEN_SCROLLED = 0.5
_log = get_logger(__name__)

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


_NON_SLOT_TAP_WORDS = frozenset(
    {
        "ok",
        "yes",
        "no",
        "back",
        "next",
        "done",
        "save",
        "cancel",
        "close",
        "open",
        "search",
        "find",
        "send",
        "submit",
        "start",
        "stop",
        "confirm",
        "allow",
        "deny",
        "accept",
        "continue",
        "skip",
        "clear",
        "delete",
        "edit",
        "add",
        "new",
        "more",
        "menu",
        "settings",
        "home",
    }
)


def _known_nav_targets(workflow: Workflow | None) -> tuple[set[str], set[str]]:
    if workflow is None:
        return set(), set()
    rids: set[str] = set()
    labels: set[str] = set()
    for e in workflow.edges:
        if e.kind == "nav":
            if e.target and e.target.resource_id:
                rids.add(e.target.resource_id)
            if e.label:
                labels.add(fold(e.label))
            if e.target and e.target.text:
                labels.add(fold(e.target.text))
    return rids, labels


def _looks_like_choice_control(node: dict[str, Any], norm: list[dict[str, Any]]) -> bool:
    """True for checkable options, list items, or peers aligned in a tab/option group."""
    if node.get("checkable") or node.get("in_scrollable") or node.get("selected"):
        return True
    cls = node.get("class") or ""
    pkg = node.get("package") or ""
    l, t, r, b = node.get("bounds") or (0, 0, 0, 0)
    for other in visible_app_nodes(norm):
        if other is node or other.get("package") != pkg or other.get("class") != cls:
            continue
        if not node_label(other):
            continue
        ol, ot, or_, ob = other.get("bounds") or (0, 0, 0, 0)
        same_row = abs(ot - t) <= 20 and abs(ob - b) <= 20
        same_col = abs(ol - l) <= 20 and abs(or_ - r) <= 20
        if same_row or same_col:
            return True
    return False


def _run_app_names(
    steps: list[RecordedStep], fg_package: str, extra_names: Iterable[str] = ()
) -> set[str]:
    """App names observed in this run (no hardcoded prepositions/keywords).

    Collects names from ``extra_names`` (e.g. ``ctx.package_cache``), recorded
    ``manage_app(app_name=...)`` calls, and Launcher/recents icon taps that
    transitioned into ``fg_package``.
    """
    names: set[str] = set()
    for a in extra_names:
        fa = fold(str(a or ""))
        if len(fa) >= 2:
            names.add(fa)
    for st in steps:
        if st.tool == "manage_app":
            raw_app = str(st.args.get("app_name") or "").strip()
            fa = fold(raw_app)
            if len(fa) >= 2:
                names.add(fa)
            continue
        if (
            fg_package
            and st.tool in TAP_TOOLS
            and st.pre is not None
            and st.post is not None
            and st.pre.elements
            and st.post.elements
        ):
            pre_norm = normalize(st.pre.elements, width=st.pre.width, height=st.pre.height)
            post_norm = normalize(st.post.elements, width=st.post.width, height=st.post.height)
            if foreground_package(pre_norm) != fg_package and foreground_package(post_norm) == fg_package:
                tapped = node_at(
                    pre_norm,
                    point=_point(st.action),
                    box=_box(st.action),
                    resource_id=str(st.action.get("target_resource_id") or ""),
                )
                for raw_lbl in (
                    node_label(tapped) if tapped is not None else "",
                    str(st.action.get("target_text") or ""),
                ):
                    fl = fold(raw_lbl)
                    if len(fl) >= 2:
                        names.add(fl)
    return names


def detect_slots(
    steps: list[RecordedStep],
    goal: str,
    *,
    workflow: Workflow | None = None,
    app_names: Iterable[str] = (),
) -> dict[str, str]:
    """Typed values or non-generic tapped option/tab labels matching whole words in ``goal``."""
    slots: dict[str, str] = {}
    g = (goal or "").casefold()
    g_fold = fold(goal)
    seen_folded: set[str] = set()
    typed_spans: list[tuple[int, int]] = []
    pkg_counts: dict[str, int] = {}
    for st in steps:
        for scr in (st.pre, st.post):
            if scr is not None and scr.elements:
                p = foreground_package(normalize(scr.elements, width=scr.width, height=scr.height))
                if p:
                    pkg_counts[p] = pkg_counts.get(p, 0) + 1
        if st.tool in INPUT_TOOLS and st.ok:
            text = str(st.args.get("text") or "").strip()
            if text and text.casefold() in g and fold(text) not in seen_folded:
                slots[f"slot{len(slots) + 1}"] = text
                seen_folded.add(fold(text))
                for m in re.finditer(re.escape(fold(text)), g_fold):
                    typed_spans.append((m.start(), m.end()))

    primary_pkg = (
        workflow.package
        if workflow is not None and workflow.package
        else (max(pkg_counts, key=lambda k: pkg_counts[k]) if pkg_counts else "")
    )
    blocked_apps = _run_app_names(steps, primary_pkg, app_names)
    nav_rids, nav_labels = _known_nav_targets(workflow)
    for st in steps:
        if st.tool not in TAP_TOOLS or not st.ok or st.no_effect or st.after_user_choice:
            continue
        if st.pre is None or not st.pre.elements:
            continue
        norm = normalize(st.pre.elements, width=st.pre.width, height=st.pre.height)
        fg = foreground_package(norm)
        node = node_at(
            norm,
            point=_point(st.action),
            box=_box(st.action),
            resource_id=str(st.action.get("target_resource_id") or ""),
        )
        if node is None or is_editable(node):
            continue
        if fg and node.get("package") != fg:
            continue
        nb = tuple(node["bounds"])
        label = node_label(node) or str(st.action.get("target_text") or "").strip()
        if is_commitment_at(norm, nb, extra_texts=(label, str(st.action.get("target_text") or ""))):  # type: ignore[arg-type]
            continue
        rid = str(node.get("resource-id") or "")
        lf = fold(label)
        if len(lf) < 3 or lf.isdigit() or lf in _NON_SLOT_TAP_WORDS or lf in blocked_apps:
            continue
        if (rid and rid in nav_rids) or lf in nav_labels:
            continue
        if lf in seen_folded:
            continue
        if not _looks_like_choice_control(node, norm):
            continue
        m = re.search(r"(?<!\w)" + re.escape(lf) + r"(?!\w)", g_fold)
        if not m:
            continue
        # Do not turn a verb/label right next to a typed slot ("add city Madrid") into a slot.
        if any(
            (ts_e <= m.start() and not g_fold[ts_e : m.start()].strip())
            or (m.end() <= ts_s and not g_fold[m.end() : ts_s].strip())
            for ts_s, ts_e in typed_spans
        ):
            continue
        slots[f"slot{len(slots) + 1}"] = label
        seen_folded.add(lf)
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


def _same_form_screens(pre: _Screen, post: _Screen) -> bool:
    """True when both screens share an identified field of a multi-field form (L-031)."""
    pre_fields = editable_field_keys(pre.norm)
    post_fields = editable_field_keys(post.norm)
    return bool(pre_fields & post_fields) and len(pre_fields | post_fields) >= 2


def _conflicts_with_other_node(wf: Workflow, nid: str, fp: ScreenFingerprint, tau: float, delta: float) -> bool:
    """True when another node already sits within ``tau - delta`` of ``fp``."""
    limit = max(0.0, tau - delta)
    for other_id, node in wf.nodes.items():
        if other_id == nid:
            continue
        if any(weighted_jaccard(fp, existing) >= limit for existing in node.fps):
            return True
    return False


def _form_node_for_dest(
    wf: Workflow,
    src: str,
    post: _Screen,
    kind: str,
    st: RecordedStep,
    target: TargetDescriptor | None,
    key: str,
    *,
    tau: float,
    delta: float,
) -> str | None:
    """Unifies a form's initial and scrolled states when the destination shows
    the form's next typing field (L-031), without lowering general tau."""
    if kind not in ("nav", "slot") or st.tool in INPUT_TOOLS:
        return None
    post_fields = editable_field_keys(post.norm)
    if not post_fields:
        return None
    target_dict = target.to_dict() if target is not None else None
    from_src_dsts = {
        e.dst
        for e in wf.edges_from(src)
        if e.dst
        and e.kind == kind
        and e.tool == st.tool
        and e.key == key
        and (e.target.to_dict() if e.target is not None else None) == target_dict
    }
    candidates: list[tuple[int, float, str]] = []
    for nid, node in wf.nodes.items():
        in_edges = [
            e
            for e in wf.edges_from(nid)
            if e.state != "disabled"
            and e.tool in INPUT_TOOLS
            and e.target is not None
            and (e.target.resource_id or e.target.hint)
        ]
        distinct_fields = {(e.target.resource_id.strip(), e.target.hint.strip()) for e in in_edges}
        if len(distinct_fields) < 2 or not field_target_matches(in_edges[0].target, post_fields):
            continue
        best_j = max((weighted_jaccard(post.fp, f) for f in node.fps), default=0.0)
        candidates.append((1 if nid in from_src_dsts else 0, best_j, nid))
    if not candidates:
        return None
    _, _, chosen = max(candidates)
    if not _conflicts_with_other_node(wf, chosen, post.fp, tau, delta):
        wf.nodes[chosen].add_fp(post.fp)
    return chosen


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
        if not is_editable(node) and is_data_label(node_label(node)):
            # A quantity / price / count shows account state, not a control the
            # task always needs: the AI decides each time (L-027).
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


def _mask_slots(text: str, values: dict[str, str]) -> str:
    out = str(text or "").strip()
    if not out:
        return ""
    for name, val in sorted(values.items(), key=lambda kv: len(kv[1]), reverse=True):
        if val and val.casefold() in out.casefold():
            idx = out.casefold().find(val.casefold())
            while idx != -1:
                out = out[:idx] + f"{{{name}}}" + out[idx + len(val) :]
                idx = out.casefold().find(val.casefold(), idx + len(name) + 2)
    return out[:120]


def _step_hints(st: RecordedStep, pre: _Screen, values: dict[str, str]) -> tuple[str, str]:
    """Returns (label, target_description) for an edge with slot values masked."""
    label = ""
    if st.tool in TAP_TOOLS:
        node = node_at(
            pre.norm,
            point=_point(st.action),
            box=_box(st.action),
            resource_id=str(st.action.get("target_resource_id") or ""),
        )
        if node is not None:
            label = node_label(node)
        if not label:
            label = str(st.action.get("target_text") or "").strip()
    elif st.tool in INPUT_TOOLS:
        fields = [n for n in visible_app_nodes(pre.norm) if is_editable(n)]
        node = node_at(pre.norm, point=_point(st.action), box=_box(st.action))
        if node is None or not is_editable(node):
            node = fields[0] if len(fields) == 1 else None
        if node is not None:
            label = str(node.get("hint") or "").strip()
    desc = str(
        st.args.get("target_description")
        or st.action.get("target_description")
        or ""
    ).strip()
    return _mask_slots(label, values), _mask_slots(desc, values)


def _is_slot_step(st: RecordedStep, pre: _Screen | None, values: dict[str, str]) -> bool:
    if st.tool in INPUT_TOOLS:
        return bool(_slot_for_text(str(st.args.get("text") or ""), values))
    if st.tool in TAP_TOOLS and pre is not None:
        node = node_at(
            pre.norm,
            point=_point(st.action),
            box=_box(st.action),
            resource_id=str(st.action.get("target_resource_id") or ""),
        )
        if node is not None and not is_editable(node):
            return bool(_slot_in_label(node_label(node), values))
    return False


def _prune_detour_cycles(
    screens: list[tuple[RecordedStep, _Screen | None, _Screen | None]],
    values: dict[str, str],
    skip: set[int],
    *,
    package: str,
) -> int:
    """Marks no-op detour cycles n_X -> ... -> n_X in ``skip``; returns pruned count."""
    effective = [
        idx
        for idx, (st, pre, _) in enumerate(screens)
        if idx not in skip and not st.autopilot and st.ok and not st.no_effect and pre is not None and pre.fp.package == package
    ]
    if not effective:
        return 0
    last_eff = effective[-1]
    pruned = 0
    i = 0
    n = len(screens)
    while i < n:
        st_i, pre_i, _ = screens[i]
        if i in skip or st_i.autopilot or not st_i.ok or st_i.no_effect or pre_i is None or pre_i.fp.package != package:
            i += 1
            continue
        pre_key = screen_content_key(st_i.pre)
        if pre_key is None:
            i += 1
            continue
        matched_k: int | None = None
        for k in range(n - 1, i, -1):
            if k in skip:
                continue
            st_k, _, post_k = screens[k]
            if st_k.autopilot or not st_k.ok or st_k.no_effect or post_k is None or post_k.fp.package != package:
                continue
            if i == 0 and k == last_eff:
                continue
            if screen_content_key(st_k.post) != pre_key:
                continue
            if any(
                j not in skip and _is_slot_step(screens[j][0], screens[j][1], values)
                for j in range(i, k + 1)
            ):
                continue
            matched_k = k
            break
        if matched_k is not None:
            for j in range(i, matched_k + 1):
                if j not in skip:
                    skip.add(j)
                    pruned += 1
            i = matched_k + 1
        else:
            i += 1
    return pruned


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


def _typed_field_key(st: RecordedStep, pre: _Screen) -> tuple[Any, ...] | None:
    """The field an input step typed into, identified the way its edge target is."""
    fields = [n for n in visible_app_nodes(pre.norm) if is_editable(n)]
    node = node_at(pre.norm, point=_point(st.action), box=_box(st.action))
    if node is None or not is_editable(node):
        node = fields[0] if len(fields) == 1 else None
    if node is None:
        return None
    d = descriptor_for(node)
    return (d.resource_id, d.hint, d.class_name, d.grid if not (d.resource_id or d.hint) else None)


def inconsistent_typing(screens: list, values: dict[str, str]) -> str:
    """Why a run's form filling cannot be trusted, or "" (L-029).

    For every task value, its LAST typing must still be in that field when the
    form is submitted (or on the most recent screen before submit that still
    displays that field, in case it scrolled out of view). Also rejects when two
    different slots' last typings on the same form landed in the same field key.
    """
    last: dict[str, int] = {}
    for i, (st, pre, _) in enumerate(screens):
        if st.tool in INPUT_TOOLS and st.ok and not st.autopilot and pre is not None:
            slot = _slot_for_text(str(st.args.get("text") or ""), values)
            if slot:
                last[slot] = i

    def _find_submit_index(start_idx: int) -> int | None:
        for k in range(start_idx + 1, len(screens)):
            st2, pre2, _ = screens[k]
            if st2.tool in INPUT_TOOLS or st2.tool in (
                "swipe",
                "wait",
                "wait_for_screen_change",
                "wait_for_text",
                "wait_until_gone",
                "wait_for_delay",
            ):
                continue
            if (
                st2.tool == "press_key"
                and str(st2.args.get("key") or "").upper() in ("BACK", "4")
                and pre2 is not None
                and keyboard_box(pre2.norm) is not None
            ):
                continue
            return k
        return None

    submit_indices: dict[str, int | None] = {slot: _find_submit_index(i) for slot, i in last.items()}
    seen_field_per_submit: dict[tuple[int | None, tuple[Any, ...]], str] = {}

    for slot, i in last.items():
        st, pre, post = screens[i]
        key = _typed_field_key(st, pre)
        sub_idx = submit_indices[slot]
        if key is not None:
            combo = (sub_idx, key)
            prev_slot = seen_field_per_submit.get(combo)
            if prev_slot is not None and fold(values[prev_slot]) != fold(values[slot]):
                label = key[1] or key[0] or key[2] or "input field"
                return (
                    f"'{values[prev_slot]}' ({prev_slot}) and '{values[slot]}' ({slot}) were both typed"
                    f" into the same field ({label})"
                )
            seen_field_per_submit[combo] = slot

        node = node_at(pre.norm, point=_point(st.action), box=_box(st.action))
        fields = [n for n in visible_app_nodes(pre.norm) if is_editable(n)]
        if node is None or not is_editable(node):
            node = fields[0] if len(fields) == 1 else None
        if node is None:
            continue
        desc = descriptor_for(node)
        desc.text = ""

        # Check submit pre-screen first; if the field scrolled off-screen by submit
        # time, walk backwards to the latest screen after step i that still shows it.
        check_screens: list[_Screen] = []
        end_k = sub_idx if sub_idx is not None else len(screens) - 1
        if sub_idx is not None and screens[sub_idx][1] is not None:
            check_screens.append(screens[sub_idx][1])
        for k in range(end_k, i - 1, -1):
            _, sk_pre, sk_post = screens[k]
            if k > i and sk_post is not None and (sub_idx is None or k < sub_idx):
                check_screens.append(sk_post)
            if k > i and sk_pre is not None and k != sub_idx:
                check_screens.append(sk_pre)
        if post is not None:
            check_screens.append(post)

        for scr in check_screens:
            field, _ = resolve(desc, scr.norm)
            if field is None:
                continue
            if fold(values[slot]) not in fold(str(field.get("text") or "")):
                label = desc.hint or desc.resource_id or desc.class_name
                return (
                    f"'{values[slot]}' ({slot}) is no longer in its field ({label}) when the form is submitted"
                    f" (it holds '{field.get('text') or ''}'): a later typing overwrote it"
                )
            break
    return ""


def record_run(
    steps: list[RecordedStep],
    *,
    goal: str,
    workflow: Workflow | None = None,
    workflow_id: str = "",
    slot_values: dict[str, str] | None = None,
    app_names: Iterable[str] = (),
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
        slots = dict(slot_values or detect_slots(steps, goal, app_names=app_names))
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

    why_inconsistent = inconsistent_typing(screens, values)
    if why_inconsistent:
        _log.warning(f"Workflow recording skipped, inconsistent run: {why_inconsistent}")
        return None

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

    # W1b: prune multi-step detours (n_X -> ... -> n_X) with no slot entered/picked
    # in between and identical screen content & element states upon return.
    _prune_detour_cycles(screens, values, skip, package=package)

    known = {e.signature(): e for e in wf.edges}
    seen_this_run: set[tuple] = set()
    added = 0
    first_src: str | None = None
    typed_on_screen: dict[str, set[str]] = {}  # node -> slots typed into its fields
    for i, (st, pre, post) in enumerate(screens):
        merged_dst: str | None = None
        kb_hide = (
            st.tool == "press_key"
            and str(st.args.get("key") or st.args.get("keycode") or "").strip().upper() in ("BACK", "4")
            and pre is not None
            and keyboard_box(pre.norm) is not None
        )
        if (
            i not in skip
            and (st.tool in ("swipe",) + tuple(INPUT_TOOLS) or kb_hide)
            and st.ok
            and pre is not None
            and post is not None
            and pre.fp.package == post.fp.package == package
            and (weighted_jaccard(pre.fp, post.fp) >= SAME_SCREEN_SCROLLED or _same_form_screens(pre, post))
        ):
            # Scrolling inside a screen (a long form), typing (keyboard
            # opening auto-scrolls a form), or hiding the keyboard over a form
            # keeps all form states in one node, so steps recorded across
            # scrolls share one node.
            nid = _node_for(wf, pre.fp, tau, delta)
            if not _conflicts_with_other_node(wf, nid, post.fp, tau, delta):
                if not match_screen(post.fp, {nid: wf.nodes[nid].fps}, tau=tau, delta=0.0, min_app_tokens=1).matched:
                    wf.nodes[nid].add_fp(post.fp)
                merged_dst = nid
            if st.tool == "swipe" or kb_hide:
                continue
        if i in skip or st.autopilot or not st.ok or st.no_effect:
            continue
        if pre is None or pre.fp.package != package:
            continue
        kind, slot, target, key = _classify(st, pre, values)
        if not kind:
            continue
        src = _node_for(wf, pre.fp, tau, delta)
        if first_src is None:
            first_src = src
        dst = None
        anchors: list[dict[str, str]] = []
        if post is not None and post.fp.package == package:
            if merged_dst is not None and st.tool in INPUT_TOOLS:
                dst = merged_dst
            else:
                fps = wf.node_fps()
                matched = match_screen(post.fp, fps, tau=tau, delta=delta, min_app_tokens=1) if fps else None
                if matched is not None and matched.matched and matched.node_id:
                    wf.nodes[matched.node_id].add_fp(post.fp)
                    dst = matched.node_id
                else:
                    if matched is None or matched.reason in ("below_tau", "no_nodes", ""):
                        dst = _form_node_for_dest(wf, src, post, kind, st, target, key, tau=tau, delta=delta)
                    if dst is None:
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
            # Every value already in this form when the step ran (visible, or
            # typed into a field of the same screen earlier and since scrolled
            # out of view): a submit recorded after all fields were filled
            # waits for all of them. Values typed on other screens do not count.
            typed = ",".join(sorted(typed_slots(pre.norm, values) | typed_on_screen.get(src, set())))
        step_label, step_desc = _step_hints(st, pre, values)
        empty_on_record = False
        if st.tool in INPUT_TOOLS and kind == "slot":
            fields = [n for n in visible_app_nodes(pre.norm) if is_editable(n)]
            node = node_at(pre.norm, point=_point(st.action), box=_box(st.action))
            if node is None or not is_editable(node):
                if len(fields) == 1:
                    node = fields[0]
                else:
                    focused = [n for n in fields if n.get("focused")]
                    node = focused[0] if len(focused) == 1 else None
            if node is not None:
                empty_on_record = not field_has_value(node, target.hint if target else "")
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
            label=step_label,
            description=step_desc,
            empty_on_record=empty_on_record,
        )
        if st.tool in INPUT_TOOLS and slot:
            typed_on_screen.setdefault(src, set()).add(slot)
        sig = edge.signature()
        if sig in known:
            existing = known[sig]
            if sig not in seen_this_run:
                existing.seen += 1
                seen_this_run.add(sig)
            if st.tool in INPUT_TOOLS:
                existing.empty_on_record = existing.empty_on_record and empty_on_record
            if not existing.label and step_label:
                existing.label = step_label
            if not existing.description and step_desc:
                existing.description = step_desc
            continue
        known[sig] = edge
        seen_this_run.add(sig)
        edge.id = wf.new_edge_id()
        wf.edges.append(edge)
        added += 1

    if workflow is None and not any(e.kind in ("nav", "slot") for e in wf.edges):
        return None
    # L-026: in workflows with a commitment action or judgement branches (where
    # later screens depend on cart / order / pre-existing state), one-off taps
    # (e.g. "Make another" when cart already held an item) must stay provisional
    # until confirmed across >= 2 independent runs.
    if wf.has_commitment() or any(e.kind == "judgement" for e in wf.edges):
        for e in wf.edges:
            if e.kind == "nav" and e.state != "disabled":
                e.state = "active" if e.seen >= 2 else "provisional"
    if first_src is not None and first_src not in wf.start_nodes:
        wf.start_nodes.append(first_src)
    # The screen the run was reported completed on (after its last action).
    for st, pre, post in reversed(screens):
        last = post if post is not None else pre
        if last is not None and last.fp.package == package:
            end = _node_for(wf, last.fp, tau, delta)
            if end not in wf.end_nodes:
                wf.end_nodes.append(end)
            break
    return wf
