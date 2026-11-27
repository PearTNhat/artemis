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

"""Offline W0 evaluator for App Workflow (recorder + autopilot over data_engine.db).

Strictly read-only on ``traces/data_engine.db`` (``?mode=ro``). Replays
completed sessions that belong to repeated task groups (>= 2 sessions with the
same package and slot-masked goal template) using a simulated device backed by
recorded pre/post UI trees.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from artemis.workflow.autopilot import Autopilot
from artemis.workflow.fingerprint import foreground_package, normalize
from artemis.workflow.recorder import (
    INPUT_TOOLS,
    NAV_KEYS,
    TAP_TOOLS,
    RawScreen,
    RecordedStep,
    detect_slots,
    record_run,
)
from artemis.workflow.targets import (
    _same_kind,
    fold,
    iou,
    is_editable,
    is_list_item,
    node_at,
    node_label,
    visible_app_nodes,
)

DEVICE_TOOLS = TAP_TOOLS | INPUT_TOOLS | frozenset({"press_key", "swipe", "click_sequence", "manage_app"})


@dataclass
class TraceSession:
    session_id: str
    start_time: float
    goal: str
    package: str
    steps: list[RecordedStep]
    slots: dict[str, str]
    template_key: str


@dataclass
class AppStats:
    groups: int = 0
    sessions: int = 0
    gt_steps: int = 0
    autopilot_steps: int = 0
    mismatches: int = 0
    undetected_mismatches: int = 0
    stop_reasons: Counter[str] = field(default_factory=Counter)


def _goal_template(goal: str, slots: dict[str, str]) -> str:
    masked = (goal or "").strip().casefold()
    for val in sorted((v for v in slots.values() if v), key=len, reverse=True):
        masked = re.sub(re.escape(val.casefold()), "<slot>", masked)
    return re.sub(r"\s+", " ", masked).strip()


def _parse_device_dims(device_info_raw: str | None) -> tuple[int | None, int | None]:
    if not device_info_raw:
        return None, None
    try:
        info = json.loads(device_info_raw)
    except ValueError:
        return None, None
    if not isinstance(info, dict):
        return None, None
    w = info.get("device_width")
    h = info.get("device_height")
    return (int(w) if w else None), (int(h) if h else None)


def _step_status(exec_raw: str | None) -> tuple[bool, bool]:
    if not exec_raw:
        return True, False
    try:
        data = json.loads(exec_raw)
    except ValueError:
        text = str(exec_raw)
        return ("error" not in text.lower()), ("(no visible change)" in text)
    if not isinstance(data, dict):
        return True, False
    status = str(data.get("status") or "").lower()
    result = str(data.get("result") or "")
    ok = status != "failed" and not result.startswith("Error")
    no_effect = "(no visible change)" in result
    return ok, no_effect


def load_completed_sessions(db_path: Path) -> list[TraceSession]:
    """Loads completed Flash/action sessions from ``data_engine.db`` in read-only mode."""
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        sessions_rows = conn.execute(
            "SELECT session_id, start_time, initial_goal, device_info "
            "FROM sessions WHERE status = 'completed' ORDER BY start_time ASC, session_id ASC"
        ).fetchall()
        images_cache: dict[str, Any] = {}

        def _tree(name: str | None) -> Any:
            if not name:
                return None
            if name not in images_cache:
                row = conn.execute("SELECT ui_tree FROM images WHERE image_name = ?", (name,)).fetchone()
                if row and row[0]:
                    try:
                        images_cache[name] = json.loads(row[0])
                    except ValueError:
                        images_cache[name] = None
                else:
                    images_cache[name] = None
            return images_cache[name]

        out: list[TraceSession] = []
        raw_sessions: list[tuple[str, Any, str, str, list[RecordedStep]]] = []
        seq_counter = 0
        for sid, start_t, goal, dev_raw in sessions_rows:
            goal = str(goal or "").strip()
            if not goal:
                continue
            w, h = _parse_device_dims(dev_raw)
            step_rows = conn.execute(
                "SELECT step_number, pre_image_name, post_image_name, action_taken, last_execution_result "
                "FROM steps WHERE session_id = ? ORDER BY step_number ASC",
                (sid,),
            ).fetchall()
            recorded: list[RecordedStep] = []
            pkg_counts: Counter[str] = Counter()
            for _, pre_name, post_name, action_raw, exec_raw in step_rows:
                if not action_raw:
                    continue
                try:
                    act = json.loads(action_raw)
                except ValueError:
                    continue
                if not isinstance(act, dict):
                    continue
                tool = str(act.get("action") or "").strip()
                if tool not in DEVICE_TOOLS:
                    continue
                args = dict(act.get("args") or {})
                ok, no_effect = _step_status(exec_raw)
                pre_tree = _tree(pre_name)
                post_tree = _tree(post_name)
                seq_counter += 1
                pre_scr = RawScreen(pre_tree, w, h, seq=seq_counter) if pre_tree else None
                seq_counter += 1
                post_scr = RawScreen(post_tree, w, h, seq=seq_counter) if post_tree else None
                if pre_scr is not None:
                    fg = foreground_package(normalize(pre_scr.elements, width=w, height=h))
                    if fg:
                        pkg_counts[fg] += 1
                recorded.append(
                    RecordedStep(
                        tool=tool,
                        args=args,
                        action=act,
                        ok=ok,
                        no_effect=no_effect,
                        pre=pre_scr,
                        post=post_scr,
                    )
                )
            if not recorded or not pkg_counts:
                continue
            pkg = pkg_counts.most_common(1)[0][0]
            raw_sessions.append((sid, start_t, goal, pkg, recorded))

        pkg_app_names: dict[str, set[str]] = defaultdict(set)
        for _, _, _, pkg, recorded in raw_sessions:
            for st in recorded:
                if st.tool == "manage_app":
                    nm = str(st.args.get("app_name") or "").strip()
                    if nm and "." not in nm:
                        pkg_app_names[pkg].add(nm)

        for sid, start_t, goal, pkg, recorded in raw_sessions:
            slots = detect_slots(recorded, goal, app_names=pkg_app_names.get(pkg, ()))
            tmpl = _goal_template(goal, slots)
            out.append(
                TraceSession(
                    session_id=str(sid),
                    start_time=float(start_t or 0.0),
                    goal=goal,
                    package=pkg,
                    steps=recorded,
                    slots=slots,
                    template_key=tmpl,
                )
            )
        return out
    finally:
        conn.close()


def _is_focus_tap(st: RecordedStep) -> bool:
    if st.tool not in TAP_TOOLS or st.pre is None:
        return False
    norm = normalize(st.pre.elements, width=st.pre.width, height=st.pre.height)
    pt = st.action.get("normalized_coordinates")
    box = st.action.get("target_bounds")
    node = node_at(
        norm,
        point=tuple(pt[:2]) if isinstance(pt, (list, tuple)) and len(pt) == 2 else None,
        box=tuple(box[:4]) if isinstance(box, (list, tuple)) and len(box) == 4 else None,
        resource_id=str(st.action.get("target_resource_id") or ""),
    )
    return node is not None and is_editable(node)


def _gt_target_node(st: RecordedStep) -> dict[str, Any] | None:
    if st.pre is None:
        return None
    norm = normalize(st.pre.elements, width=st.pre.width, height=st.pre.height)
    pt = st.action.get("normalized_coordinates")
    box = st.action.get("target_bounds")
    return node_at(
        norm,
        point=tuple(pt[:2]) if isinstance(pt, (list, tuple)) and len(pt) == 2 else None,
        box=tuple(box[:4]) if isinstance(box, (list, tuple)) and len(box) == 4 else None,
        resource_id=str(st.action.get("target_resource_id") or ""),
    )


def _same_node(el: dict[str, Any] | None, gt_node: dict[str, Any] | None, gt_action: dict[str, Any]) -> bool:
    if el is None:
        return False
    eb = tuple(int(x) for x in (el.get("bounds") or (0, 0, 0, 0)))
    if gt_node is not None:
        gb = tuple(int(x) for x in (gt_node.get("bounds") or (0, 0, 0, 0)))
        if eb == gb or iou(eb, gb) >= 0.5:
            return True
        if el.get("resource-id") and el.get("resource-id") == gt_node.get("resource-id"):
            if not node_label(el) or fold(node_label(el)) == fold(node_label(gt_node)):
                return True
    gt_box = gt_action.get("target_bounds")
    if isinstance(gt_box, (list, tuple)) and len(gt_box) == 4:
        gb2 = tuple(int(x) for x in gt_box)
        if eb == gb2 or iou(eb, gb2) >= 0.5:
            return True
    gt_pt = gt_action.get("normalized_coordinates")
    if isinstance(gt_pt, (list, tuple)) and len(gt_pt) == 2:
        px, py = int(gt_pt[0]), int(gt_pt[1])
        if eb[0] <= px <= eb[2] and eb[1] <= py <= eb[3]:
            return True
    return False


def _gt_actual_input_node(gt: RecordedStep) -> dict[str, Any] | None:
    """Resolves the field that actually received ``input_text`` in ``gt``.

    When the historical AI trace passed a non-editable overlay or keyboard view as
    ``target`` while typing went into the focused ``EditText`` (confirmed by ``gt.post``),
    resolve to that actual ``EditText`` rather than the hallucinated hint/overlay node.
    """
    gt_node = _gt_target_node(gt)
    if gt_node is not None and is_editable(gt_node):
        return gt_node
    if gt.pre is None:
        return gt_node
    pre_norm = normalize(gt.pre.elements, width=gt.pre.width, height=gt.pre.height)
    fields = [n for n in visible_app_nodes(pre_norm) if is_editable(n)]
    typed = fold(str(gt.args.get("text") or ""))
    if gt.post is not None and typed:
        post_norm = normalize(gt.post.elements, width=gt.post.width, height=gt.post.height)
        post_hits = [
            pn for pn in visible_app_nodes(post_norm) if is_editable(pn) and typed in fold(str(pn.get("text") or ""))
        ]
        if len(post_hits) == 1:
            ph = post_hits[0]
            matched = [
                fn
                for fn in fields
                if (ph.get("resource-id") and fn.get("resource-id") == ph.get("resource-id"))
                or iou(
                    tuple(int(x) for x in (fn.get("bounds") or (0, 0, 0, 0))),
                    tuple(int(x) for x in (ph.get("bounds") or (0, 0, 0, 0))),
                )
                >= 0.5
            ]
            if len(matched) == 1:
                return matched[0]
    focused = [n for n in fields if n.get("focused")]
    if len(focused) == 1:
        return focused[0]
    if len(fields) == 1:
        return fields[0]
    return gt_node


def _matches_ground_truth(
    tool: str, args: dict[str, Any], element: dict[str, Any] | None, gt: RecordedStep
) -> bool:
    if tool in TAP_TOOLS:
        if gt.tool not in TAP_TOOLS:
            return False
        return _same_node(element, _gt_target_node(gt), gt.action)
    if tool in INPUT_TOOLS:
        if gt.tool not in INPUT_TOOLS:
            return False
        if fold(str(args.get("text") or "")) != fold(str(gt.args.get("text") or "")):
            return False
        gt_node = _gt_actual_input_node(gt)
        if element is None:
            return gt_node is not None and bool(gt_node.get("focused"))
        if gt_node is None:
            return False
        return _same_node(element, gt_node, gt.action)
    if tool == "press_key":
        if gt.tool != "press_key":
            return False
        k1 = str(args.get("key") or args.get("keycode") or "").strip().upper()
        k2 = str(gt.args.get("key") or gt.args.get("keycode") or "").strip().upper()
        alias = {"66": "ENTER", "84": "SEARCH", "4": "BACK"}
        return alias.get(k1, k1) == alias.get(k2, k2)
    return False


def _simulate_wrong_input_screen(pre: RawScreen, element: dict[str, Any] | None, typed_text: str) -> RawScreen:
    """Builds a realistic post-screen where ``typed_text`` landed in ``element`` (the wrong field)."""
    norm = normalize(pre.elements, width=pre.width, height=pre.height)
    target_bounds = tuple(int(x) for x in (element or {}).get("bounds") or (0, 0, 0, 0))
    for n in norm:
        if element is not None and tuple(int(x) for x in (n.get("bounds") or (0, 0, 0, 0))) == target_bounds:
            n["text"] = typed_text
            break
    return RawScreen(norm, width=1000, height=1000, seq=(pre.seq or 0) + 1)


def _is_peer_list_mismatch(gt: RecordedStep, element: dict[str, Any] | None) -> bool:
    if element is None or gt.pre is None or gt.tool not in TAP_TOOLS:
        return False
    norm = normalize(gt.pre.elements, width=gt.pre.width, height=gt.pre.height)
    gt_node = _gt_target_node(gt)
    if gt_node is None:
        return False
    if not (is_list_item(gt_node, norm) or is_list_item(element, norm)):
        return False
    peers = _same_kind(gt_node, norm)
    eb = tuple(int(x) for x in (element.get("bounds") or (0, 0, 0, 0)))
    return any(tuple(int(x) for x in (p.get("bounds") or (0, 0, 0, 0))) == eb for p in peers)


class _OfflineTraceDriver:
    """Fake device backed by a session's recorded pre/post UI trees."""

    def __init__(self, gt_steps: list[RecordedStep]):
        self.gt_steps = gt_steps
        self.idx = 0
        self.screen: RawScreen | None = gt_steps[0].pre if gt_steps else None
        self.matched = 0
        self.mismatches = 0
        self.mismatch_details: list[str] = []
        self._mismatch_pending: int | None = None

    def current(self) -> RawScreen | None:
        if self._mismatch_pending is not None:
            self._mismatch_pending += 1
        return self.screen

    async def refresh(self) -> RawScreen | None:
        return self.screen

    async def act(
        self, tool: str, args: dict[str, Any], element: dict[str, Any] | None
    ) -> tuple[bool, str]:
        self._mismatch_pending = None
        if self.idx >= len(self.gt_steps):
            self.mismatches += 1
            self.mismatch_details.append(f"past_end:{tool}")
            return False, "offline: no more recorded steps"

        # Skip optional focus-only tap on the target field right before input_text.
        if tool in INPUT_TOOLS and _is_focus_tap(self.gt_steps[self.idx]) and self.idx + 1 < len(self.gt_steps):
            if self.gt_steps[self.idx + 1].tool in INPUT_TOOLS:
                self.idx += 1

        gt = self.gt_steps[self.idx]

        # Auxiliary recovery steps (keyboard hide / swipe search): only advance if trace also did them.
        if tool == "swipe" or (tool == "press_key" and str(args.get("key") or "").upper() == "BACK" and gt.tool != "press_key"):
            if gt.tool == tool:
                self.screen = gt.post
                self.idx += 1
                return True, "offline aux matched"
            return False, f"offline: aux {tool} not in ground truth"

        if _matches_ground_truth(tool, args, element, gt):
            self.matched += 1
            self.screen = gt.post
            self.idx += 1
            return True, "offline matched"

        self.mismatches += 1
        self.mismatch_details.append(f"step{self.idx + 1}:ap={tool}({args}) vs gt={gt.tool}({gt.args})")
        # Non-optimistic mismatch simulation (L-022):
        # - wrong input_text field: put text into the field Autopilot actually targeted
        # - wrong tap / key: transition to gt.post (what the real AI action actually led to)
        #   so we expose any wrong tap whose destination still passes _verified().
        if tool in INPUT_TOOLS and gt.pre is not None:
            self.screen = _simulate_wrong_input_screen(gt.pre, element, str(args.get("text") or ""))
        elif gt.post is not None:
            self.screen = gt.post
            self.idx += 1
            self._mismatch_pending = 0
        return True, "offline mismatch simulated"

    def advance_ground_truth_step(self) -> bool:
        self._mismatch_pending = None
        if self.idx >= len(self.gt_steps):
            return False
        self.screen = self.gt_steps[self.idx].post
        self.idx += 1
        return True


def _effective_steps(sess: TraceSession) -> list[RecordedStep]:
    out: list[RecordedStep] = []
    for st in sess.steps:
        if not st.ok or st.no_effect or st.pre is None or st.post is None:
            continue
        if st.tool in TAP_TOOLS | INPUT_TOOLS | frozenset({"press_key", "swipe"}):
            out.append(st)
    return out


def _align_slots(wf_slots: dict[str, str], sess_slots: dict[str, str]) -> dict[str, str]:
    aligned: dict[str, str] = {}
    wf_keys = list(wf_slots.keys())
    sess_vals = list(sess_slots.values())
    for i, k in enumerate(wf_keys):
        if k in sess_slots:
            aligned[k] = sess_slots[k]
        elif i < len(sess_vals):
            aligned[k] = sess_vals[i]
    return aligned


async def evaluate_db(db_path: Path, *, verbose: bool = False) -> dict[str, AppStats]:
    sessions = load_completed_sessions(db_path)
    groups: dict[tuple[str, str], list[TraceSession]] = defaultdict(list)
    for s in sessions:
        groups[(s.package, s.template_key)].append(s)

    by_app: dict[str, AppStats] = defaultdict(AppStats)
    for (pkg, tmpl), grp in sorted(groups.items(), key=lambda kv: (kv[0][0], -len(kv[1]), kv[0][1])):
        if len(grp) < 2:
            continue
        wf = record_run(grp[0].steps, goal=grp[0].goal)
        if wf is None:
            continue
        app = by_app[pkg]
        app.groups += 1
        if verbose:
            print(f"\n[GROUP] {pkg} ({len(grp)} sessions) goal={tmpl[:90]}")

        for sess in grp[1:]:
            gt_steps = _effective_steps(sess)
            if not gt_steps:
                continue
            app.sessions += 1
            app.gt_steps += len(gt_steps)
            slot_values = _align_slots(wf.slots, sess.slots)
            ap = Autopilot(wf, slot_values, verify_polls=1, poll_interval_s=0.0)
            drv = _OfflineTraceDriver(gt_steps)
            sess_matched = 0
            sess_mismatches = 0
            sess_undetected = 0
            stops: list[str] = []

            while drv.idx < len(gt_steps):
                before_idx = drv.idx
                matched_before = drv.matched
                mismatches_before = drv.mismatches
                drv._mismatch_pending = None
                rep = await ap.run(drv)
                matched_now = drv.matched - matched_before
                mismatched_now = drv.mismatches - mismatches_before
                verified_non_aux = sum(
                    1
                    for r in rep.ran
                    if not r.startswith(("swipe up", "press_key BACK (hide the keyboard"))
                )
                if mismatched_now > 0:
                    sess_mismatches += mismatched_now
                    if verified_non_aux > matched_now:
                        sess_undetected += verified_non_aux - matched_now
                if rep.stop_reason:
                    stops.append(rep.stop_reason)
                sess_matched = drv.matched
                # Advance one non-autopilot ground-truth step when Autopilot handed over
                # without already moving drv.screen to the failed step's gt.post.
                if drv.idx == before_idx:
                    if not drv.advance_ground_truth_step():
                        break
                elif drv.idx < len(gt_steps) and not rep.finished:
                    if drv._mismatch_pending != 1:
                        if not drv.advance_ground_truth_step():
                            break
                else:
                    break

            app.autopilot_steps += sess_matched
            app.mismatches += sess_mismatches
            app.undetected_mismatches += sess_undetected
            for sr in stops:
                app.stop_reasons[sr] += 1
            if verbose:
                print(
                    f"  - {sess.session_id[:8]}: {sess_matched}/{len(gt_steps)} steps, "
                    f"mismatches={sess_mismatches} (undetected={sess_undetected}), stops={stops[:2]}"
                )
                for md in drv.mismatch_details:
                    print(f"      MISMATCH: {md}")

            merged = record_run(sess.steps, goal=sess.goal, workflow=wf, slot_values=slot_values)
            if merged is not None:
                wf = merged

    return dict(by_app)


def print_report(by_app: dict[str, AppStats]) -> None:
    print("\n=== W0 OFFLINE WORKFLOW REPLAY EVALUATION ===")
    header = (
        f"{'Package':<34} {'Grp':>3} {'Sess':>4} {'GT':>4} {'AP':>4} "
        f"{'Cov%':>6} {'Mis':>4} {'Undet':>5}  Top Stop Reasons"
    )
    print(header)
    print("-" * len(header))
    tot = AppStats()
    for pkg, st in sorted(by_app.items()):
        cov = (100.0 * st.autopilot_steps / st.gt_steps) if st.gt_steps else 0.0
        top = "; ".join(f"{k} ({v})" for k, v in st.stop_reasons.most_common(2))
        print(
            f"{pkg:<34} {st.groups:>3} {st.sessions:>4} {st.gt_steps:>4} {st.autopilot_steps:>4} "
            f"{cov:>5.1f}% {st.mismatches:>4} {st.undetected_mismatches:>5}  {top}"
        )
        tot.groups += st.groups
        tot.sessions += st.sessions
        tot.gt_steps += st.gt_steps
        tot.autopilot_steps += st.autopilot_steps
        tot.mismatches += st.mismatches
        tot.undetected_mismatches += st.undetected_mismatches
        tot.stop_reasons.update(st.stop_reasons)
    cov_tot = (100.0 * tot.autopilot_steps / tot.gt_steps) if tot.gt_steps else 0.0
    print("-" * len(header))
    print(
        f"{'TOTAL':<34} {tot.groups:>3} {tot.sessions:>4} {tot.gt_steps:>4} {tot.autopilot_steps:>4} "
        f"{cov_tot:>5.1f}% {tot.mismatches:>4} {tot.undetected_mismatches:>5}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline W0 replay evaluator on data_engine.db.")
    parser.add_argument("--db", type=Path, default=Path("traces/data_engine.db"))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    by_app = asyncio.run(evaluate_db(args.db, verbose=args.verbose))
    print_report(by_app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
