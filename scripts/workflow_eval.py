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

"""Offline M0 evaluation script for screen fingerprinting and matching (Mục 2.4).

Reads ``traces/data_engine.db`` strictly in read-only mode, normalizes every
``images.ui_tree`` through ``artemis.workflow.fingerprint.normalize``, splits
sessions 60/40, labels positive/negative screen pairs, sweeps ``tau`` on the
calibration split for matcher ``false_match = 0`` (respecting Condition 1
package equality), simulates session-to-session replay on the held-out test
split (at both ``tau_cal`` and ``DEFAULT_TAU = 0.80``), and exports 50 random
labeled pairs with image paths for pending human visual inspection.

Note on Mục 5.1 offline limitation: offline evaluation checks commitment
keywords on UI node ``text``/``content-desc`` and ``action_taken`` text only;
runtime screenshot OCR around the tap point cannot run offline without live
inference and is deferred to M2.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import random
import sqlite3
from typing import Any, Iterable, Sequence

from artemis.mcp.action_executor import is_commitment_text
from artemis.workflow.fingerprint import (
    SYSTEMUI_PACKAGE,
    ScreenFingerprint,
    normalize,
    tokenize,
)
from artemis.workflow.matcher import (
    DEFAULT_DELTA,
    DEFAULT_TAU,
    MIN_APP_TOKENS,
    match_screen,
    weighted_jaccard,
)

SINGLE_TARGET_ACTIONS = frozenset(
    {"click", "tap", "input_text", "focus_and_input_text", "long_press"}
)
# Containers covering > 40% of the normalized 1000x1000 canvas are root/layout
# wrappers rather than actionable controls and must not be replayed as targets.
MAX_TARGET_AREA_NORM = 400_000


@dataclass(frozen=True)
class TappedElement:
    """Normalized representation of the UI element targeted by an AI step."""

    action: str
    element_key: tuple[Any, ...] | None
    resource_id: str
    class_name: str
    text: str
    bounds: tuple[int, int, int, int]
    grid: tuple[int, int]
    is_commitment: bool


@dataclass(frozen=True)
class EvalStep:
    """One trace step paired with its normalized pre-action UI screen."""

    session_id: str
    step_number: int
    image_name: str
    image_path: str
    norm_nodes: list[dict[str, Any]]
    fingerprint: ScreenFingerprint
    tapped: TappedElement | None
    initial_goal: str = ""
    action_name: str = ""
    sequence_first_tapped: TappedElement | None = None


@dataclass(frozen=True)
class LabeledPair:
    """Labeled pair of steps for calibration/test ROC evaluation."""

    label: int  # 1 = positive (same screen), 0 = negative (different screen)
    reason: str
    step_a: EvalStep
    step_b: EvalStep
    rid_jaccard: float
    similarity: float


def _bounds_area(box: tuple[int, int, int, int]) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _bounds_intersection(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    l = max(a[0], b[0])
    t = max(a[1], b[1])
    r = min(a[2], b[2])
    b_ = min(a[3], b[3])
    if r <= l or b_ <= t:
        return 0
    return (r - l) * (b_ - t)


def _bounds_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    inter = _bounds_intersection(a, b)
    if inter <= 0:
        return 0.0
    area_a = max(1, _bounds_area(a))
    area_b = max(1, _bounds_area(b))
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _bounds_min_containment(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Returns intersection divided by min(area_a, area_b) to detect label-vs-card overlap."""
    inter = _bounds_intersection(a, b)
    if inter <= 0:
        return 0.0
    min_area = max(1, min(_bounds_area(a), _bounds_area(b)))
    return inter / min_area


def _action_family(action: str) -> str:
    if action in ("click", "tap"):
        return "tap"
    if action in ("input_text", "focus_and_input_text"):
        return "input"
    return action


def _visible_app_nodes(norm_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for node in norm_nodes:
        if node.get("package") == SYSTEMUI_PACKAGE:
            continue
        if not node.get("visible-to-user", True):
            continue
        l, t, r, b = node.get("bounds") or (0, 0, 0, 0)
        if r <= l or b <= t:
            continue
        result.append(node)
    return result


def _scale_box_to_1000(
    box: list[Any] | tuple[Any, ...],
    coord_space: str | None,
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        l, t, r, b = [float(v) for v in box]
    except (TypeError, ValueError):
        return None
    if coord_space == "pixel" and width > 0 and height > 0:
        l = l * 1000.0 / width
        t = t * 1000.0 / height
        r = r * 1000.0 / width
        b = b * 1000.0 / height
    return (
        max(0, min(1000, int(round(l)))),
        max(0, min(1000, int(round(t)))),
        max(0, min(1000, int(round(r)))),
        max(0, min(1000, int(round(b)))),
    )


def _scale_point_to_1000(
    pt: list[Any] | tuple[Any, ...],
    coord_space: str | None,
    width: int,
    height: int,
) -> tuple[int, int] | None:
    if not isinstance(pt, (list, tuple)) or len(pt) < 2:
        return None
    try:
        x, y = float(pt[0]), float(pt[1])
    except (TypeError, ValueError):
        return None
    if coord_space == "pixel" and width > 0 and height > 0:
        x = x * 1000.0 / width
        y = y * 1000.0 / height
    return (max(0, min(1000, int(round(x)))), max(0, min(1000, int(round(y)))))


def _is_node_commitment(
    norm_nodes: list[dict[str, Any]],
    target_box: tuple[int, int, int, int],
    extra_texts: Iterable[str] = (),
) -> bool:
    """Checks Mục 5.1 commitment text on UI nodes (offline text/content-desc only)."""
    for txt in extra_texts:
        if txt and is_commitment_text(txt):
            return True
    cx = (target_box[0] + target_box[2]) // 2
    cy = (target_box[1] + target_box[3]) // 2
    for node in _visible_app_nodes(norm_nodes):
        nl, nt, nr, nb = node["bounds"]
        node_box = (nl, nt, nr, nb)
        contains_center = nl <= cx <= nr and nt <= cy <= nb
        overlaps_target = _bounds_iou(node_box, target_box) >= 0.5
        inside_target = (
            nl >= target_box[0] - 5
            and nt >= target_box[1] - 5
            and nr <= target_box[2] + 5
            and nb <= target_box[3] + 5
        )
        if (contains_center and (node.get("clickable") or overlaps_target)) or inside_target or overlaps_target:
            for txt in (node.get("text") or "", node.get("content-desc") or ""):
                if txt and is_commitment_text(txt):
                    return True
    return False


def _extract_action_name(action_taken_raw: str | None) -> str:
    """Extracts the primary action name from ``steps.action_taken`` JSON."""
    if not action_taken_raw:
        return ""
    try:
        parsed = json.loads(action_taken_raw)
    except json.JSONDecodeError:
        return ""
    act_obj = parsed[0] if isinstance(parsed, list) and parsed else parsed
    if not isinstance(act_obj, dict):
        return ""
    return str(act_obj.get("action") or "").strip()


def extract_sequence_first_element(
    action_taken_raw: str | None,
    norm_nodes: list[dict[str, Any]],
    width: int,
    height: int,
) -> TappedElement | None:
    """Resolves the first tap in a ``click_sequence`` per Mục 3.2, or None if unresolvable."""
    if not action_taken_raw:
        return None
    try:
        parsed = json.loads(action_taken_raw)
    except json.JSONDecodeError:
        return None
    act_obj = parsed[0] if isinstance(parsed, list) and parsed else parsed
    if not isinstance(act_obj, dict) or str(act_obj.get("action") or "") != "click_sequence":
        return None

    args = act_obj.get("args") if isinstance(act_obj.get("args"), dict) else {}
    coords = act_obj.get("coordinates") or args.get("sequence") or []
    if not isinstance(coords, list) or not coords:
        return None
    first_pt = coords[0]
    descs = act_obj.get("target_descriptions") or args.get("target_descriptions") or []
    first_desc = str(descs[0]).strip() if isinstance(descs, list) and descs else ""
    coord_space = act_obj.get("coordinate_space")
    synthetic_click = {
        "action": "click",
        "coordinate_space": coord_space,
        "coordinates": first_pt,
        "normalized_coordinates": first_pt if coord_space == "normalized" else None,
        "target_description": first_desc,
    }
    return extract_tapped_element(json.dumps(synthetic_click), norm_nodes, width, height)


def extract_tapped_element(
    action_taken_raw: str | None,
    norm_nodes: list[dict[str, Any]],
    width: int,
    height: int,
) -> TappedElement | None:
    """Infers the targeted normalized UI node from ``steps.action_taken``.

    Rejects root/oversized containers (> 40% screen area) so coordinate taps
    without a specific UI node do not silently bind to screen-wide wrappers.
    """
    if not action_taken_raw:
        return None
    try:
        parsed = json.loads(action_taken_raw)
    except json.JSONDecodeError:
        return None

    act_obj = parsed[0] if isinstance(parsed, list) and parsed else parsed
    if not isinstance(act_obj, dict):
        return None
    action_name = str(act_obj.get("action") or "")
    if action_name not in SINGLE_TARGET_ACTIONS:
        return None

    coord_space = act_obj.get("coordinate_space")
    norm_coords = act_obj.get("normalized_coordinates")
    raw_coords = act_obj.get("coordinates")
    tap_pt = (
        _scale_point_to_1000(norm_coords, "normalized", width, height)
        if norm_coords is not None
        else _scale_point_to_1000(raw_coords, coord_space, width, height)
    )
    target_box = _scale_box_to_1000(act_obj.get("target_bounds"), coord_space, width, height)
    target_rid = str(act_obj.get("target_resource_id") or "").strip()
    target_txt = str(act_obj.get("target_text") or "").strip()

    visible = [
        n
        for n in _visible_app_nodes(norm_nodes)
        if _bounds_area(tuple(n["bounds"])) <= MAX_TARGET_AREA_NORM
    ]
    matched_node: dict[str, Any] | None = None

    if target_box is not None:
        scored: list[tuple[float, int, int, int, dict[str, Any]]] = []
        for node in visible:
            nbox = tuple(node["bounds"])
            iou = _bounds_iou(target_box, nbox)
            if iou >= 0.5:
                rid_match = int(bool(target_rid and node["resource-id"] == target_rid))
                txt_match = int(
                    bool(target_txt and target_txt in (node["text"], node["content-desc"]))
                )
                scored.append((iou, rid_match, txt_match, -_bounds_area(nbox), node))
        if scored:
            scored.sort(key=lambda item: item[:4], reverse=True)
            matched_node = scored[0][4]
    elif tap_pt is not None:
        px, py = tap_pt
        candidates: list[tuple[int, int, int, dict[str, Any]]] = []
        for node in visible:
            nl, nt, nr, nb = node["bounds"]
            if nl <= px <= nr and nt <= py <= nb:
                rid_match = int(bool(target_rid and node["resource-id"] == target_rid))
                clk_pref = int(bool(node.get("clickable") or node.get("editable")))
                candidates.append((rid_match, clk_pref, -_bounds_area((nl, nt, nr, nb)), node))
        if candidates:
            candidates.sort(key=lambda item: item[:3], reverse=True)
            matched_node = candidates[0][3]

    if matched_node is not None:
        rid = matched_node["resource-id"] or target_rid
        cls = matched_node["class"] or str(act_obj.get("target_class") or "").strip()
        txt = matched_node["text"] or matched_node["content-desc"] or target_txt
        box = tuple(matched_node["bounds"])
    elif target_box is not None and _bounds_area(target_box) <= MAX_TARGET_AREA_NORM:
        rid = target_rid
        cls = str(act_obj.get("target_class") or "").strip()
        txt = target_txt
        box = target_box
    else:
        return None

    if _bounds_area(box) > MAX_TARGET_AREA_NORM:
        return None

    cx = (box[0] + box[2]) // 2
    cy = (box[1] + box[3]) // 2
    grid = (min(9, max(0, cx // 100)), min(9, max(0, cy // 100)))
    if rid:
        elem_key: tuple[Any, ...] | None = ("rid", rid)
    elif txt:
        elem_key = ("text_grid", txt, grid[0], grid[1])
    else:
        elem_key = None

    commitment = _is_node_commitment(norm_nodes, box, extra_texts=(target_txt, txt))
    return TappedElement(
        action=action_name,
        element_key=elem_key,
        resource_id=rid,
        class_name=cls,
        text=txt,
        bounds=box,
        grid=grid,
        is_commitment=commitment,
    )


def rid_jaccard(fp_a: ScreenFingerprint, fp_b: ScreenFingerprint) -> float:
    """Unweighted Jaccard over screen resource-id sets (Mục 2.4)."""
    a = fp_a.resource_ids
    b = fp_b.resource_ids
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def detect_global_elements(
    steps: Iterable[EvalStep],
    *,
    min_distinct_screens: int = 3,
    distinct_threshold: float = 0.6,
) -> dict[str, set[tuple[Any, ...]]]:
    """Finds per-app global elements appearing across >=3 distinct screens (Mục 2.4)."""
    appearances: dict[str, dict[tuple[Any, ...], list[ScreenFingerprint]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for step in steps:
        fp = step.fingerprint
        if not fp.package or fp.app_token_count < MIN_APP_TOKENS:
            continue
        seen_on_screen: set[tuple[Any, ...]] = set()
        for node in _visible_app_nodes(step.norm_nodes):
            rid = node["resource-id"]
            if rid:
                seen_on_screen.add(("rid", rid))
            else:
                txt = node["text"] or node["content-desc"]
                if txt:
                    l, t, r, b = node["bounds"]
                    gx = min(9, max(0, ((l + r) // 2) // 100))
                    gy = min(9, max(0, ((t + b) // 2) // 100))
                    seen_on_screen.add(("text_grid", txt, gx, gy))
        for key in seen_on_screen:
            appearances[fp.package][key].append(fp)

    globals_by_app: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    for pkg, key_map in appearances.items():
        for key, fps in key_map.items():
            distinct_reps: list[ScreenFingerprint] = []
            for fp in fps:
                if all(rid_jaccard(fp, rep) < distinct_threshold for rep in distinct_reps):
                    distinct_reps.append(fp)
                    if len(distinct_reps) >= min_distinct_screens:
                        globals_by_app[pkg].add(key)
                        break
    return dict(globals_by_app)


def build_labeled_pairs(
    steps: list[EvalStep],
    globals_by_app: dict[str, set[tuple[Any, ...]]],
    *,
    max_cross_app_negatives: int = 3000,
    seed: int = 42,
) -> tuple[list[LabeledPair], dict[str, int]]:
    """Labels step pairs according to Mục 2.4 rules without touching the gray zone."""
    valid_steps = [
        s for s in steps if s.fingerprint.package and s.fingerprint.app_token_count >= MIN_APP_TOKENS
    ]
    positives: list[LabeledPair] = []
    same_app_negatives: list[LabeledPair] = []
    cross_app_pool: list[LabeledPair] = []
    stats = {
        "valid_steps": len(valid_steps),
        "skipped_sparse_or_no_pkg": len(steps) - len(valid_steps),
        "skipped_gray_zone": 0,
        "skipped_global_button": 0,
        "skipped_same_session_consecutive": 0,
    }

    by_pkg: dict[str, list[EvalStep]] = defaultdict(list)
    for s in valid_steps:
        by_pkg[s.fingerprint.package].append(s)

    for pkg, pkg_steps in by_pkg.items():
        pkg_globals = globals_by_app.get(pkg, set())
        n = len(pkg_steps)
        for i in range(n):
            sa = pkg_steps[i]
            for j in range(i + 1, n):
                sb = pkg_steps[j]
                if sa.session_id == sb.session_id:
                    if abs(sa.step_number - sb.step_number) <= 1:
                        stats["skipped_same_session_consecutive"] += 1
                    continue
                rj = rid_jaccard(sa.fingerprint, sb.fingerprint)
                sim = weighted_jaccard(sa.fingerprint, sb.fingerprint)
                if rj < 0.3:
                    same_app_negatives.append(
                        LabeledPair(0, "same_app_rid_lt_0.3", sa, sb, rj, sim)
                    )
                elif rj < 0.6:
                    stats["skipped_gray_zone"] += 1
                else:
                    if (
                        sa.tapped is not None
                        and sb.tapped is not None
                        and sa.tapped.element_key is not None
                        and sa.tapped.element_key == sb.tapped.element_key
                    ):
                        if sa.tapped.element_key in pkg_globals:
                            stats["skipped_global_button"] += 1
                        else:
                            positives.append(
                                LabeledPair(1, "same_app_same_non_global_elem", sa, sb, rj, sim)
                            )

    pkgs = sorted(by_pkg.keys())
    for idx_a in range(len(pkgs)):
        for idx_b in range(idx_a + 1, len(pkgs)):
            steps_a = by_pkg[pkgs[idx_a]]
            steps_b = by_pkg[pkgs[idx_b]]
            for sa in steps_a:
                for sb in steps_b:
                    rj = rid_jaccard(sa.fingerprint, sb.fingerprint)
                    sim = weighted_jaccard(sa.fingerprint, sb.fingerprint)
                    cross_app_pool.append(
                        LabeledPair(0, "cross_app", sa, sb, rj, sim)
                    )

    rng = random.Random(seed)
    if len(cross_app_pool) > max_cross_app_negatives:
        cross_app_negatives = rng.sample(cross_app_pool, max_cross_app_negatives)
    else:
        cross_app_negatives = cross_app_pool

    all_pairs = positives + same_app_negatives + cross_app_negatives
    stats["positives"] = len(positives)
    stats["same_app_negatives"] = len(same_app_negatives)
    stats["cross_app_negatives"] = len(cross_app_negatives)
    return all_pairs, stats


def _similarity_distribution(sims: Sequence[float]) -> dict[str, Any]:
    if not sims:
        return {"count": 0, "max": 0.0, "p95": 0.0, "p99": 0.0, "ge_0_80": 0}
    ordered = sorted(sims)
    n = len(ordered)
    return {
        "count": n,
        "max": round(ordered[-1], 4),
        "p95": round(ordered[min(n - 1, int(0.95 * (n - 1)))], 4),
        "p99": round(ordered[min(n - 1, int(0.99 * (n - 1)))], 4),
        "ge_0_80": sum(1 for v in ordered if v >= 0.80),
    }


def calibrate_tau(pairs: list[LabeledPair]) -> dict[str, Any]:
    """Sweeps tau in [0.50, 0.99] using the actual matcher condition (same package + S >= tau).

    Cross-app pairs are always rejected by Condition (1) of ``match_screen``, so
    their structural similarity is reported separately as a fingerprint
    discriminability diagnostic rather than counted as matcher false positives.
    """
    positives = [p for p in pairs if p.label == 1]
    same_app_neg = [
        p
        for p in pairs
        if p.label == 0 and p.step_a.fingerprint.package == p.step_b.fingerprint.package
    ]
    cross_app_neg = [
        p
        for p in pairs
        if p.label == 0 and p.step_a.fingerprint.package != p.step_b.fingerprint.package
    ]

    best_tau = None
    best_recall = 0.0
    sweep: list[dict[str, Any]] = []

    for step_i in range(50, 100):
        tau = round(step_i / 100.0, 2)
        tp = sum(
            1
            for p in positives
            if p.step_a.fingerprint.package == p.step_b.fingerprint.package and p.similarity >= tau
        )
        fp_same = sum(1 for p in same_app_neg if p.similarity >= tau)
        # Matcher rejects different packages via Condition (1), so matcher cross-app FP = 0.
        fp_cross_matcher = 0
        cross_sim_ge_tau = sum(1 for p in cross_app_neg if p.similarity >= tau)
        recall = tp / len(positives) if positives else 0.0
        sweep.append(
            {
                "tau": tau,
                "tp": tp,
                "fp_same_app": fp_same,
                "fp_cross_app_matcher": fp_cross_matcher,
                "cross_app_sim_ge_tau": cross_sim_ge_tau,
                "recall": round(recall, 4),
            }
        )
        if best_tau is None and fp_same == 0:
            best_tau = tau
            best_recall = recall

    if best_tau is None:
        best_tau = 0.99
        best_recall = sweep[-1]["recall"] if sweep else 0.0
        zero_fp_achieved = False
    else:
        zero_fp_achieved = True

    selected_row = next((r for r in sweep if r["tau"] == best_tau), sweep[-1])
    return {
        "selected_tau": best_tau,
        "zero_false_match_achieved": zero_fp_achieved,
        "recall": round(best_recall, 4),
        "false_match_same_app": selected_row["fp_same_app"],
        "false_match_cross_app_matcher": selected_row["fp_cross_app_matcher"],
        "cross_app_sim_ge_selected_tau": selected_row["cross_app_sim_ge_tau"],
        "same_app_negative_sim_dist": _similarity_distribution([p.similarity for p in same_app_neg]),
        "cross_app_similarity_dist": _similarity_distribution([p.similarity for p in cross_app_neg]),
        "calibration_positives": len(positives),
        "calibration_negatives_same_app": len(same_app_neg),
        "calibration_negatives_cross_app": len(cross_app_neg),
        "sweep": sweep,
    }


def _is_occluded(
    target_node: dict[str, Any],
    visible_nodes: list[dict[str, Any]],
) -> bool:
    """Checks whether a later sibling/overlay covers >= 50% of ``target_node``."""
    try:
        idx = visible_nodes.index(target_node)
    except ValueError:
        return False
    tl, tt, tr, tb = target_node["bounds"]
    t_area = max(1, (tr - tl) * (tb - tt))
    for later in visible_nodes[idx + 1 :]:
        ll, lt, lr, lb = later["bounds"]
        if ll >= tl - 2 and lt >= tt - 2 and lr <= tr + 2 and lb <= tb + 2:
            continue
        il = max(tl, ll)
        it = max(tt, lt)
        ir = min(tr, lr)
        ib = min(tb, lb)
        if ir > il and ib > it:
            if ((ir - il) * (ib - it)) / t_area >= 0.5:
                return True
    return False


def _resolve_target_on_screen(
    edge_target: TappedElement,
    norm_nodes: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """Resolves a recorded edge target uniquely and unoccluded on a live screen (3.4 + 5.1)."""
    visible = _visible_app_nodes(norm_nodes)
    matches: list[dict[str, Any]] = []

    if edge_target.resource_id:
        rid_matches = [n for n in visible if n["resource-id"] == edge_target.resource_id]
        if len(rid_matches) == 1:
            matches = rid_matches
        elif len(rid_matches) > 1:
            grid_matches = [
                n
                for n in rid_matches
                if (
                    min(9, max(0, ((n["bounds"][0] + n["bounds"][2]) // 2) // 100)),
                    min(9, max(0, ((n["bounds"][1] + n["bounds"][3]) // 2) // 100)),
                )
                == edge_target.grid
            ]
            if len(grid_matches) == 1:
                matches = grid_matches
            else:
                return None, "ambiguous_rid"
    if not matches and edge_target.text:
        txt_matches = [
            n for n in visible if edge_target.text in (n["text"], n["content-desc"])
        ]
        if len(txt_matches) == 1:
            matches = txt_matches
        elif len(txt_matches) > 1:
            return None, "ambiguous_text"
    if not matches and edge_target.class_name:
        grid_cls_matches = [
            n
            for n in visible
            if (n.get("clickable") or n.get("editable"))
            and n["class"] == edge_target.class_name
            and (
                min(9, max(0, ((n["bounds"][0] + n["bounds"][2]) // 2) // 100)),
                min(9, max(0, ((n["bounds"][1] + n["bounds"][3]) // 2) // 100)),
            )
            == edge_target.grid
        ]
        if len(grid_cls_matches) == 1:
            matches = grid_cls_matches

    if len(matches) != 1:
        return None, "not_found"

    target_node = matches[0]
    if _bounds_area(tuple(target_node["bounds"])) > MAX_TARGET_AREA_NORM:
        return None, "oversized_container"
    if _is_occluded(target_node, visible):
        return None, "occluded"
    if _is_node_commitment(norm_nodes, tuple(target_node["bounds"])):
        return target_node, "commitment_blocked"
    return target_node, "ok"


def resolve_node_edges(
    edges: Sequence[TappedElement],
    norm_nodes: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, TappedElement | None, str]:
    """Resolves all candidate edges from a matched node per Mục 3.5.

    Only returns ``ok`` when exactly one distinct (action_family, resource_id, bounds)
    target resolves on the live screen. Multiple distinct resolvable targets fall
    back with reason ``multi_edge``.
    """
    ok_candidates: dict[
        tuple[str, str, tuple[int, int, int, int]],
        tuple[dict[str, Any], TappedElement],
    ] = {}
    reasons: list[str] = []

    for edge in edges:
        node, status = _resolve_target_on_screen(edge, norm_nodes)
        if status == "ok" and node is not None:
            key = (
                _action_family(edge.action),
                str(node.get("resource-id") or ""),
                tuple(node["bounds"]),
            )
            ok_candidates[key] = (node, edge)
        else:
            reasons.append(status)

    if "commitment_blocked" in reasons and len(ok_candidates) <= 1:
        return None, None, "commitment_blocked"
    if len(ok_candidates) == 1:
        node, edge = next(iter(ok_candidates.values()))
        return node, edge, "ok"
    if len(ok_candidates) > 1:
        return None, None, "multi_edge"
    if "oversized_container" in reasons:
        return None, None, "oversized_container"
    if "occluded" in reasons:
        return None, None, "occluded"
    if any(r in ("ambiguous_rid", "ambiguous_text") for r in reasons):
        return None, None, "ambiguous_target"
    return None, None, "not_found"


def _same_action_target(proposed_node: dict[str, Any], actual: TappedElement) -> bool:
    """Primary Mục 2.4 agreement: identical resource-id or bounds IoU >= 0.5."""
    if actual.resource_id and proposed_node["resource-id"] == actual.resource_id:
        return True
    return _bounds_iou(tuple(proposed_node["bounds"]), actual.bounds) >= 0.5


def _same_action_target_containment(proposed_node: dict[str, Any], actual: TappedElement) -> bool:
    """Diagnostic agreement allowing label-inside-card overlap (intersection/min_area >= 0.5)."""
    if _same_action_target(proposed_node, actual):
        return True
    return _bounds_min_containment(tuple(proposed_node["bounds"]), actual.bounds) >= 0.5


def _classify_proposal_error(
    proposed_node: dict[str, Any],
    proposed_edge: TappedElement,
    actual: TappedElement,
) -> str:
    pbox = tuple(proposed_node["bounds"])
    abox = actual.bounds
    if _bounds_area(pbox) > MAX_TARGET_AREA_NORM or _bounds_area(abox) > MAX_TARGET_AREA_NORM:
        return "oversized_container"
    if _bounds_min_containment(pbox, abox) >= 0.5:
        return "label_vs_card"
    if _action_family(proposed_edge.action) != _action_family(actual.action):
        return "different_action"
    return "stage_or_choice_mismatch"


def simulate_test_workflows(
    test_sessions: dict[str, list[EvalStep]],
    *,
    tau: float,
    delta: float,
    same_goal_only: bool = False,
) -> dict[str, Any]:
    """Simulates building a workflow from session A and replaying on session B (Mục 2.4)."""
    by_pkg: dict[str, list[str]] = defaultdict(list)
    session_goal: dict[str, str] = {}
    for sid, steps in test_sessions.items():
        counts: dict[str, int] = defaultdict(int)
        goal_val = ""
        for s in steps:
            if not goal_val and s.initial_goal:
                goal_val = s.initial_goal.strip()
            if s.fingerprint.package and s.fingerprint.app_token_count >= MIN_APP_TOKENS:
                counts[s.fingerprint.package] += 1
        session_goal[sid] = goal_val
        if counts:
            dom = max(counts, key=lambda p: counts[p])
            by_pkg[dom].append(sid)

    total_steps = 0
    total_tap_steps = 0
    unknown_target_steps = 0
    proposed_steps = 0
    proposed_on_tap_steps = 0
    unknown_target_proposals = 0
    correct_proposals = 0
    correct_on_tap_steps = 0
    correct_proposals_containment = 0
    commitment_blocked = 0
    commitment_proposals = 0
    actual_commitment_steps = 0
    proposed_on_actual_commitment = 0
    cross_app_false_matches = 0
    session_pairs_evaluated = 0
    fallback_reasons: Counter[str] = Counter()
    error_breakdown: Counter[str] = Counter()

    for pkg, sids in sorted(by_pkg.items()):
        if len(sids) < 2:
            continue
        for sid_a in sids:
            nodes_fps: dict[str, list[ScreenFingerprint]] = {}
            node_edges: dict[str, list[TappedElement]] = {}
            for step_a in test_sessions[sid_a]:
                if (
                    step_a.tapped is None
                    or step_a.tapped.is_commitment
                    or step_a.fingerprint.package != pkg
                    or step_a.fingerprint.app_token_count < MIN_APP_TOKENS
                ):
                    continue
                merged_id = None
                if nodes_fps:
                    m = match_screen(step_a.fingerprint, nodes_fps, tau=tau, delta=delta)
                    if m.matched:
                        merged_id = m.node_id
                if merged_id is None:
                    merged_id = f"n{len(nodes_fps) + 1}"
                    nodes_fps[merged_id] = [step_a.fingerprint]
                    node_edges[merged_id] = [step_a.tapped]
                else:
                    if len(nodes_fps[merged_id]) < 5:
                        nodes_fps[merged_id].append(step_a.fingerprint)
                    node_edges[merged_id].append(step_a.tapped)

            if not nodes_fps:
                continue

            for sid_b in sids:
                if sid_b == sid_a:
                    continue
                if same_goal_only:
                    ga, gb = session_goal.get(sid_a, ""), session_goal.get(sid_b, "")
                    if not ga or ga != gb:
                        continue
                session_pairs_evaluated += 1
                for step_b in test_sessions[sid_b]:
                    if step_b.fingerprint.app_token_count < MIN_APP_TOKENS:
                        continue
                    total_steps += 1
                    is_tap_step = step_b.tapped is not None
                    if is_tap_step:
                        total_tap_steps += 1
                        actual_target: TappedElement | None = step_b.tapped
                    elif (
                        step_b.action_name == "click_sequence"
                        and step_b.sequence_first_tapped is not None
                    ):
                        actual_target = step_b.sequence_first_tapped
                    else:
                        actual_target = None
                        if step_b.action_name in SINGLE_TARGET_ACTIONS:
                            unknown_target_steps += 1

                    if actual_target is not None and actual_target.is_commitment:
                        actual_commitment_steps += 1

                    resolved_cache: dict[str, tuple[dict[str, Any] | None, TappedElement | None, str]] = {}

                    def _resolver(nid: str) -> bool:
                        node, edge, status = resolve_node_edges(
                            node_edges.get(nid, []), step_b.norm_nodes
                        )
                        resolved_cache[nid] = (node, edge, status)
                        return status == "ok" and node is not None

                    res = match_screen(
                        step_b.fingerprint,
                        nodes_fps,
                        tau=tau,
                        delta=delta,
                        target_resolver=_resolver,
                    )
                    if not res.matched:
                        if res.reason == "target_unresolved" and resolved_cache:
                            sub_reason = next(iter(resolved_cache.values()))[2]
                            fallback_reasons[sub_reason] += 1
                            if sub_reason == "commitment_blocked":
                                commitment_blocked += 1
                        else:
                            fallback_reasons[res.reason] += 1
                        continue

                    if res.node_id is None or res.node_id not in resolved_cache:
                        continue
                    prop_node, prop_edge, _ = resolved_cache[res.node_id]
                    if prop_node is None or prop_edge is None:
                        continue
                    if _is_node_commitment(step_b.norm_nodes, tuple(prop_node["bounds"])):
                        commitment_proposals += 1
                    proposed_steps += 1
                    if is_tap_step:
                        proposed_on_tap_steps += 1
                    if actual_target is not None and actual_target.is_commitment:
                        proposed_on_actual_commitment += 1

                    if actual_target is not None:
                        if _same_action_target(prop_node, actual_target):
                            correct_proposals += 1
                            correct_proposals_containment += 1
                            if is_tap_step:
                                correct_on_tap_steps += 1
                        else:
                            if _same_action_target_containment(prop_node, actual_target):
                                correct_proposals_containment += 1
                            err_cat = _classify_proposal_error(prop_node, prop_edge, actual_target)
                            error_breakdown[err_cat] += 1
                    else:
                        if step_b.action_name in SINGLE_TARGET_ACTIONS:
                            unknown_target_proposals += 1
                            error_breakdown["unknown_target"] += 1
                        else:
                            act_label = step_b.action_name or "none"
                            error_breakdown[f"ai_other_action:{act_label}"] += 1

            if not same_goal_only:
                for other_pkg, other_sids in by_pkg.items():
                    if other_pkg == pkg:
                        continue
                    for sid_other in other_sids:
                        for step_o in test_sessions[sid_other]:
                            if step_o.fingerprint.app_token_count < MIN_APP_TOKENS:
                                continue
                            m_other = match_screen(
                                step_o.fingerprint, nodes_fps, tau=tau, delta=delta
                            )
                            if m_other.matched:
                                cross_app_false_matches += 1

    coverage = (proposed_steps / total_steps) if total_steps else 0.0
    precision = (correct_proposals / proposed_steps) if proposed_steps else 0.0
    precision_containment = (
        correct_proposals_containment / proposed_steps if proposed_steps else 0.0
    )
    precision_on_tap_steps = (
        correct_on_tap_steps / proposed_on_tap_steps if proposed_on_tap_steps else 0.0
    )
    coverage_on_tap_steps = (
        proposed_on_tap_steps / total_tap_steps if total_tap_steps else 0.0
    )
    return {
        "scope": "same_goal" if same_goal_only else "same_app",
        "session_pairs_evaluated": session_pairs_evaluated,
        "total_target_steps": total_steps,
        "total_tap_steps": total_tap_steps,
        "unknown_target_steps": unknown_target_steps,
        "proposed_steps": proposed_steps,
        "proposed_on_tap_steps": proposed_on_tap_steps,
        "unknown_target_proposals": unknown_target_proposals,
        "correct_proposals": correct_proposals,
        "correct_on_tap_steps": correct_on_tap_steps,
        "correct_proposals_containment": correct_proposals_containment,
        "coverage": round(coverage, 4),
        "precision": round(precision, 4),
        "precision_containment": round(precision_containment, 4),
        "coverage_on_tap_steps": round(coverage_on_tap_steps, 4),
        "precision_on_tap_steps": round(precision_on_tap_steps, 4),
        "llm_calls_saved": correct_proposals,
        "commitment_blocked": commitment_blocked,
        "commitment_proposals": commitment_proposals,
        "actual_commitment_steps": actual_commitment_steps,
        "proposed_on_actual_commitment": proposed_on_actual_commitment,
        "cross_app_false_matches": cross_app_false_matches,
        "fallback_reasons": dict(sorted(fallback_reasons.items())),
        "error_breakdown": dict(sorted(error_breakdown.items())),
    }


def load_steps_from_db(db_path: Path, images_dir: Path) -> tuple[list[str], list[EvalStep], dict[str, int]]:
    """Loads sessions and steps from DataEngine SQLite DB in read-only mode."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sessions = [
            row
            for row in conn.execute(
                "SELECT session_id, initial_goal, device_info FROM sessions ORDER BY start_time, session_id"
            ).fetchall()
        ]
        dev_dims: dict[str, tuple[int, int]] = {}
        session_goals: dict[str, str] = {}
        session_ids: list[str] = []
        for sid, goal_raw, di_raw in sessions:
            session_ids.append(sid)
            session_goals[sid] = str(goal_raw or "").strip()
            di = json.loads(di_raw) if di_raw else {}
            dw = int(di.get("device_width") or 1080)
            dh = int(di.get("device_height") or 2400)
            dev_dims[sid] = (dw, dh)

        img_rows = conn.execute("SELECT ui_tree FROM images").fetchall()
        empty_images = sum(
            1
            for (u,) in img_rows
            if not u or str(u).strip() in ("", "[]", "null")
        )

        rows = conn.execute(
            """
            SELECT s.session_id, s.step_number, s.pre_image_name, s.action_taken, i.ui_tree
            FROM steps s
            LEFT JOIN images i ON s.pre_image_name = i.image_name
            ORDER BY s.session_id, s.step_number
            """
        ).fetchall()
    finally:
        conn.close()

    eval_steps: list[EvalStep] = []
    counts = {
        "total_images": len(img_rows),
        "empty_ui_tree_images": empty_images,
        "total_db_steps": len(rows),
        "skipped_empty_ui_tree_steps": 0,
    }
    for sid, step_num, pre_img, act_raw, ui_raw in rows:
        if not ui_raw:
            counts["skipped_empty_ui_tree_steps"] += 1
            continue
        try:
            raw_tree = json.loads(ui_raw)
        except json.JSONDecodeError:
            counts["skipped_empty_ui_tree_steps"] += 1
            continue
        if not raw_tree:
            counts["skipped_empty_ui_tree_steps"] += 1
            continue

        dw, dh = dev_dims.get(sid, (1080, 2400))
        norm = normalize(raw_tree, width=dw, height=dh)
        fp = tokenize(norm)
        act_name = _extract_action_name(act_raw)
        tapped = extract_tapped_element(act_raw, norm, dw, dh)
        seq_first = (
            extract_sequence_first_element(act_raw, norm, dw, dh)
            if act_name == "click_sequence"
            else None
        )
        img_path = str(images_dir / f"{pre_img}.jpg") if pre_img else ""
        eval_steps.append(
            EvalStep(
                session_id=sid,
                step_number=int(step_num),
                image_name=str(pre_img or ""),
                image_path=img_path,
                norm_nodes=norm,
                fingerprint=fp,
                tapped=tapped,
                initial_goal=session_goals.get(sid, ""),
                action_name=act_name,
                sequence_first_tapped=seq_first,
            )
        )
    return session_ids, eval_steps, counts


def split_sessions(
    session_ids: list[str], *, train_ratio: float = 0.6, seed: int = 42
) -> tuple[set[str], set[str]]:
    """Splits session IDs deterministically without ever mixing a session's steps."""
    shuffled = sorted(session_ids)
    random.Random(seed).shuffle(shuffled)
    cut = int(round(len(shuffled) * train_ratio))
    return set(shuffled[:cut]), set(shuffled[cut:])


def export_sample_pairs(
    pairs: list[LabeledPair], out_path: Path, *, sample_size: int = 50, seed: int = 42
) -> list[dict[str, Any]]:
    """Exports up to ``sample_size`` stratified random labeled pairs with image paths."""
    rng = random.Random(seed)
    positives = [p for p in pairs if p.label == 1]
    negatives = [p for p in pairs if p.label == 0]
    n_pos = min(len(positives), sample_size // 2)
    n_neg = min(len(negatives), sample_size - n_pos)
    chosen = rng.sample(positives, n_pos) + rng.sample(negatives, n_neg)
    rng.shuffle(chosen)
    payload = [
        {
            "label": "positive" if p.label == 1 else "negative",
            "reason": p.reason,
            "similarity": round(p.similarity, 4),
            "rid_jaccard": round(p.rid_jaccard, 4),
            "step_a": {
                "session_id": p.step_a.session_id,
                "step_number": p.step_a.step_number,
                "package": p.step_a.fingerprint.package,
                "image_path": p.step_a.image_path,
                "tapped_text": p.step_a.tapped.text if p.step_a.tapped else None,
                "tapped_rid": p.step_a.tapped.resource_id if p.step_a.tapped else None,
            },
            "step_b": {
                "session_id": p.step_b.session_id,
                "step_number": p.step_b.step_number,
                "package": p.step_b.fingerprint.package,
                "image_path": p.step_b.image_path,
                "tapped_text": p.step_b.tapped.text if p.step_b.tapped else None,
                "tapped_rid": p.step_b.tapped.resource_id if p.step_b.tapped else None,
            },
        }
        for p in chosen
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def _eval_pairs_at_tau(pairs: list[LabeledPair], tau: float) -> dict[str, Any]:
    pos = [p for p in pairs if p.label == 1]
    same_neg = [
        p for p in pairs if p.label == 0 and p.step_a.fingerprint.package == p.step_b.fingerprint.package
    ]
    cross_neg = [
        p for p in pairs if p.label == 0 and p.step_a.fingerprint.package != p.step_b.fingerprint.package
    ]
    tp = sum(
        1
        for p in pos
        if p.step_a.fingerprint.package == p.step_b.fingerprint.package and p.similarity >= tau
    )
    fp_same = sum(1 for p in same_neg if p.similarity >= tau)
    cross_sim_ge_tau = sum(1 for p in cross_neg if p.similarity >= tau)
    return {
        "tau": tau,
        "pair_recall": round(tp / len(pos), 4) if pos else 0.0,
        "pair_false_match_same_app": fp_same,
        "pair_false_match_cross_app_matcher": 0,
        "pair_cross_app_sim_ge_tau": cross_sim_ge_tau,
        "cross_app_similarity_dist": _similarity_distribution([p.similarity for p in cross_neg]),
    }


def run_evaluation(
    db_path: Path,
    images_dir: Path,
    *,
    seed: int = 42,
    train_ratio: float = 0.6,
    delta: float = DEFAULT_DELTA,
    sample_out: Path | None = None,
) -> dict[str, Any]:
    """Runs the complete M0 offline calibration and test evaluation pipeline."""
    session_ids, all_steps, load_counts = load_steps_from_db(db_path, images_dir)
    cal_sids, test_sids = split_sessions(session_ids, train_ratio=train_ratio, seed=seed)

    cal_steps = [s for s in all_steps if s.session_id in cal_sids]
    test_steps = [s for s in all_steps if s.session_id in test_sids]

    globals_cal = detect_global_elements(cal_steps)
    globals_test = detect_global_elements(test_steps)

    cal_pairs, cal_pair_stats = build_labeled_pairs(cal_steps, globals_cal, seed=seed)
    test_pairs, test_pair_stats = build_labeled_pairs(test_steps, globals_test, seed=seed)

    cal_result = calibrate_tau(cal_pairs)
    tau_cal = cal_result["selected_tau"]

    test_by_session: dict[str, list[EvalStep]] = defaultdict(list)
    for s in test_steps:
        test_by_session[s.session_id].append(s)

    sim_cal_same_app = simulate_test_workflows(test_by_session, tau=tau_cal, delta=delta, same_goal_only=False)
    sim_cal_same_goal = simulate_test_workflows(test_by_session, tau=tau_cal, delta=delta, same_goal_only=True)
    sim_def_same_app = simulate_test_workflows(test_by_session, tau=DEFAULT_TAU, delta=delta, same_goal_only=False)
    sim_def_same_goal = simulate_test_workflows(test_by_session, tau=DEFAULT_TAU, delta=delta, same_goal_only=True)

    sample_file = sample_out or Path("traces/workflow_eval_samples.json")
    exported = export_sample_pairs(cal_pairs + test_pairs, sample_file, sample_size=50, seed=seed)

    def _gate(sim: dict[str, Any]) -> bool:
        return (
            sim["precision"] >= 0.98
            and sim["commitment_proposals"] == 0
            and sim["cross_app_false_matches"] == 0
        )

    return {
        "db_path": str(db_path),
        "seed": seed,
        "offline_limitations": {
            "commitment_5_1": "Offline evaluation checks UI node text/content-desc and action_taken text only; screenshot OCR around tap point is deferred to M2.",
            "precision_metric_definition": (
                "Primary gate precision uses ALL valid-fingerprint B steps (app_token_count >= 5) "
                "as denominator, requiring same resource-id or bounds IoU >= 0.5 (click_sequence "
                "compared against its resolved first element; non-single-target actions and "
                "unextracted single-target taps count as wrong proposals). precision_on_tap_steps "
                "(only steps with resolved single-target tap) and precision_containment "
                "(intersection / min_area >= 0.5) are reported alongside as diagnostics only."
            ),
            "sample_visual_audit_status": "PENDING_HUMAN_REVIEW",
        },
        "split": {
            "total_sessions": len(session_ids),
            "calibration_sessions": len(cal_sids),
            "test_sessions": len(test_sids),
            **load_counts,
        },
        "calibration_pairs": cal_pair_stats,
        "calibration_roc": cal_result,
        "test_pairs": {
            **test_pair_stats,
            "at_calibrated_tau": _eval_pairs_at_tau(test_pairs, tau_cal),
            "at_default_tau": _eval_pairs_at_tau(test_pairs, DEFAULT_TAU),
        },
        "test_simulation": {
            "tau": tau_cal,
            "delta": delta,
            **sim_cal_same_app,
        },
        "test_simulation_same_goal": {
            "tau": tau_cal,
            "delta": delta,
            **sim_cal_same_goal,
        },
        "test_simulation_default_tau": {
            "tau": DEFAULT_TAU,
            "delta": delta,
            **sim_def_same_app,
        },
        "test_simulation_default_tau_same_goal": {
            "tau": DEFAULT_TAU,
            "delta": delta,
            **sim_def_same_goal,
        },
        "sample_audit": {
            "count": len(exported),
            "path": str(sample_file),
            "human_audit_status": "PENDING_HUMAN_REVIEW",
        },
        "m2_gate_passed": _gate(sim_cal_same_app),
        "m2_gate_passed_default_tau": _gate(sim_def_same_app),
    }


def _print_sim_line(label: str, sim: dict[str, Any]) -> None:
    print(
        f"- {label} (tau={sim['tau']}, delta={sim['delta']}, scope={sim['scope']}): "
        f"pairs={sim['session_pairs_evaluated']}, steps={sim['total_target_steps']} "
        f"(tap={sim['total_tap_steps']}, unknown_target={sim['unknown_target_steps']}), "
        f"proposed={sim['proposed_steps']} (tap={sim['proposed_on_tap_steps']}, "
        f"unknown_target_prop={sim['unknown_target_proposals']}), "
        f"correct={sim['correct_proposals']}, "
        f"coverage={sim['coverage']:.4f}, precision(gate)={sim['precision']:.4f}, "
        f"precision_on_tap_steps(diag)={sim['precision_on_tap_steps']:.4f}, "
        f"precision_containment(diag)={sim['precision_containment']:.4f}, "
        f"LLM_calls_saved={sim['llm_calls_saved']}, "
        f"commitment_proposals={sim['commitment_proposals']}, "
        f"commitment_blocked={sim['commitment_blocked']}, "
        f"actual_commitment_steps={sim['actual_commitment_steps']}, "
        f"proposed_on_actual_commitment={sim['proposed_on_actual_commitment']}, "
        f"cross_app_false_matches={sim['cross_app_false_matches']}"
    )
    if sim["error_breakdown"]:
        print(f"  - error_breakdown: {sim['error_breakdown']}")
    if sim["fallback_reasons"]:
        print(f"  - fallback_reasons: {sim['fallback_reasons']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline M0 workflow fingerprint evaluation.")
    parser.add_argument("--db", type=Path, default=Path("traces/data_engine.db"))
    parser.add_argument("--images-dir", type=Path, default=Path("traces/images"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--train-ratio", type=float, default=0.6)
    parser.add_argument("--delta", type=float, default=DEFAULT_DELTA)
    parser.add_argument("--sample-out", type=Path, default=Path("traces/workflow_eval_samples.json"))
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args()

    report = run_evaluation(
        args.db,
        args.images_dir,
        seed=args.seed,
        train_ratio=args.train_ratio,
        delta=args.delta,
        sample_out=args.sample_out,
    )
    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    sp = report["split"]
    cal_p = report["calibration_pairs"]
    roc = report["calibration_roc"]
    tp = report["test_pairs"]
    print("# M0 Offline Workflow Evaluation")
    print(f"- DB: `{report['db_path']}` (read-only), seed={report['seed']}")
    print(
        f"- Split: {sp['calibration_sessions']} calibration / {sp['test_sessions']} test sessions "
        f"(images: {sp['empty_ui_tree_images']}/{sp['total_images']} empty ui_tree; "
        f"steps: {sp['skipped_empty_ui_tree_steps']}/{sp['total_db_steps']} skipped due to empty ui_tree)"
    )
    print(
        f"- Calibration pairs: positive={cal_p['positives']}, "
        f"same_app_neg={cal_p['same_app_negatives']}, cross_app_neg={cal_p['cross_app_negatives']} "
        f"(gray_zone_skipped={cal_p['skipped_gray_zone']}, global_skipped={cal_p['skipped_global_button']})"
    )
    print(
        f"- Calibration ROC (matcher Condition 1 same-package + S>=tau): "
        f"selected_tau={roc['selected_tau']} (zero_false_match={roc['zero_false_match_achieved']}), "
        f"recall={roc['recall']:.4f}, fp_same_app={roc['false_match_same_app']}, "
        f"fp_cross_app_matcher={roc['false_match_cross_app_matcher']}"
    )
    print(
        f"- Diagnostic negative similarity distributions (without package guard): "
        f"same_app={roc['same_app_negative_sim_dist']}, "
        f"cross_app={roc['cross_app_similarity_dist']}"
    )
    print("- Calibration ROC sweep (selected thresholds):")
    print("  | tau | tp | fp_same_app | fp_cross_matcher | cross_sim>=tau | recall |")
    print("  | --- | -- | ----------- | ---------------- | -------------- | ------ |")
    key_taus = {0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.82, 0.85, 0.90, 0.95, 0.99, roc["selected_tau"]}
    for row in roc["sweep"]:
        if row["tau"] in key_taus:
            print(
                f"  | {row['tau']:.2f} | {row['tp']} | {row['fp_same_app']} | "
                f"{row['fp_cross_app_matcher']} | {row['cross_app_sim_ge_tau']} | {row['recall']:.4f} |"
            )

    tp_cal = tp["at_calibrated_tau"]
    tp_def = tp["at_default_tau"]
    print(
        f"- Test pairs: positive={tp['positives']}, same_app_neg={tp['same_app_negatives']}, "
        f"cross_app_neg={tp['cross_app_negatives']}"
    )
    print(
        f"  - at tau_cal={tp_cal['tau']}: recall={tp_cal['pair_recall']:.4f}, "
        f"fp_same={tp_cal['pair_false_match_same_app']}, "
        f"fp_cross_matcher={tp_cal['pair_false_match_cross_app_matcher']}, "
        f"cross_sim>=tau={tp_cal['pair_cross_app_sim_ge_tau']}"
    )
    print(
        f"  - at tau_default={tp_def['tau']}: recall={tp_def['pair_recall']:.4f}, "
        f"fp_same={tp_def['pair_false_match_same_app']}, "
        f"fp_cross_matcher={tp_def['pair_false_match_cross_app_matcher']}, "
        f"cross_sim>=tau={tp_def['pair_cross_app_sim_ge_tau']}"
    )

    _print_sim_line("Test simulation (calibrated tau, all same-app)", report["test_simulation"])
    _print_sim_line("Test simulation (calibrated tau, same-goal)", report["test_simulation_same_goal"])
    _print_sim_line("Test simulation (default tau=0.80, all same-app)", report["test_simulation_default_tau"])
    _print_sim_line("Test simulation (default tau=0.80, same-goal)", report["test_simulation_default_tau_same_goal"])
    print(
        f"- Sample pairs exported: {report['sample_audit']['count']} -> "
        f"`{report['sample_audit']['path']}` (status: {report['sample_audit']['human_audit_status']})"
    )
    print(f"- Offline 5.1 note: {report['offline_limitations']['commitment_5_1']}")
    print(
        f"- M2 Gate (precision>=0.98, commitment=0, cross_app_fp=0): "
        f"at_tau_cal={report['m2_gate_passed']}, at_default_0.80={report['m2_gate_passed_default_tau']}"
    )


if __name__ == "__main__":
    main()
