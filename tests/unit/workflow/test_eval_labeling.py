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

"""Unit tests for Mục 2.4 offline pair labeling, calibration, and safety checks."""

import json

from artemis.workflow.fingerprint import normalize, tokenize
from scripts.workflow_eval import (
    EvalStep,
    TappedElement,
    _resolve_target_on_screen,
    build_labeled_pairs,
    calibrate_tau,
    detect_global_elements,
    extract_sequence_first_element,
    extract_tapped_element,
    resolve_node_edges,
    rid_jaccard,
    simulate_test_workflows,
    split_sessions,
)


def _make_step(
    sid: str,
    step_num: int,
    pkg: str,
    rids: list[str],
    tapped_rid: str | None = None,
    tapped_text: str = "",
) -> EvalStep:
    raw = [
        {
            "class": "android.widget.Button",
            "package": pkg,
            "resource-id": f"{pkg}:id/{rid}",
            "bounds": f"[50,{100 + i * 110}][950,{190 + i * 110}]",
            "clickable": "true",
            "visible-to-user": "true",
            "text": tapped_text if rid == tapped_rid else f"lbl_{rid}",
        }
        for i, rid in enumerate(rids)
    ]
    norm = normalize(raw, width=1000, height=1000)
    fp = tokenize(norm)
    tapped = None
    if tapped_rid is not None:
        idx = rids.index(tapped_rid)
        action = json.dumps(
            {
                "action": "click",
                "coordinate_space": "normalized",
                "normalized_coordinates": [500, 145 + idx * 110],
                "target_bounds": [50, 100 + idx * 110, 950, 190 + idx * 110],
                "target_resource_id": f"{pkg}:id/{tapped_rid}",
                "target_text": tapped_text,
            }
        )
        tapped = extract_tapped_element(action, norm, 1000, 1000)
    return EvalStep(
        session_id=sid,
        step_number=step_num,
        image_name=f"{sid}_{step_num}",
        image_path=f"traces/images/{sid}_{step_num}.jpg",
        norm_nodes=norm,
        fingerprint=fp,
        tapped=tapped,
    )


def test_split_sessions_never_mixes_same_session():
    sids = [f"s{i}" for i in range(10)]
    cal, test = split_sessions(sids, train_ratio=0.6, seed=42)
    assert len(cal) == 6
    assert len(test) == 4
    assert cal.isdisjoint(test)
    assert cal | test == set(sids)


def test_global_button_excluded_from_positives_and_labeling_zones():
    # Three distinct screens in com.app sharing "nav_back" (global button)
    s1_a = _make_step("s1", 1, "com.app", ["nav_back", "a1", "a2", "a3", "a4"], tapped_rid="nav_back")
    s1_b = _make_step("s1", 2, "com.app", ["nav_back", "b1", "b2", "b3", "b4"], tapped_rid="b1")
    s1_c = _make_step("s1", 3, "com.app", ["nav_back", "c1", "c2", "c3", "c4"], tapped_rid="c1")

    # Session 2 visits screen A (taps nav_back) and screen A again (taps a1)
    s2_a_back = _make_step("s2", 1, "com.app", ["nav_back", "a1", "a2", "a3", "a4"], tapped_rid="nav_back")
    s2_a_item = _make_step("s2", 2, "com.app", ["nav_back", "a1", "a2", "a3", "a4"], tapped_rid="a1")
    s1_a_item = _make_step("s1", 4, "com.app", ["nav_back", "a1", "a2", "a3", "a4"], tapped_rid="a1")

    # Gray zone screen: shares 2 of 5 rids -> 2/8 = 0.25 or 3/7 = 0.428 in [0.3, 0.6)
    s2_gray = _make_step("s2", 3, "com.app", ["nav_back", "a1", "a2", "g1", "g2"], tapped_rid="a1")
    assert 0.3 <= rid_jaccard(s1_a_item.fingerprint, s2_gray.fingerprint) < 0.6

    # Different app negative
    s2_other = _make_step("s2", 4, "com.other", ["x1", "x2", "x3", "x4", "x5"], tapped_rid="x1")

    steps = [s1_a, s1_b, s1_c, s1_a_item, s2_a_back, s2_a_item, s2_gray, s2_other]
    globals_map = detect_global_elements(steps)
    assert ("rid", "com.app:id/nav_back") in globals_map["com.app"]

    pairs, stats = build_labeled_pairs(steps, globals_map)
    pos = [p for p in pairs if p.label == 1]
    neg = [p for p in pairs if p.label == 0]
    assert len(pos) == 1
    assert pos[0].step_a.tapped.resource_id == "com.app:id/a1"
    assert stats["skipped_global_button"] >= 1
    assert stats["skipped_gray_zone"] >= 1
    assert any(p.reason == "cross_app" for p in neg)
    assert any(p.reason == "same_app_rid_lt_0.3" for p in neg)


def test_calibrate_tau_and_commitment_detection():
    s1 = _make_step("s1", 1, "com.app", ["a1", "a2", "a3", "a4", "a5"], tapped_rid="a1")
    s2 = _make_step("s2", 1, "com.app", ["a1", "a2", "a3", "a4", "a5"], tapped_rid="a1")
    s3 = _make_step("s2", 2, "com.app", ["b1", "b2", "b3", "b4", "b5"], tapped_rid="b1")
    pairs, _ = build_labeled_pairs([s1, s2, s3], {})
    roc = calibrate_tau(pairs)
    assert roc["zero_false_match_achieved"] is True
    assert roc["recall"] == 1.0

    # Commitment check uses repo's is_commitment_text (Vietnamese & English)
    commit_step = _make_step(
        "s3", 1, "com.app", ["a1", "a2", "a3", "a4", "pay"], tapped_rid="pay", tapped_text="Đặt đơn"
    )
    assert commit_step.tapped is not None
    assert commit_step.tapped.is_commitment is True


def test_calibrate_tau_respects_package_condition_and_reports_cross_app_diag():
    s1 = _make_step("s1", 1, "com.app1", ["a1", "a2", "a3", "a4", "a5"], tapped_rid="a1")
    s2 = _make_step("s2", 1, "com.app1", ["a1", "a2", "a3", "a4", "a5"], tapped_rid="a1")
    # Different app with identical unlabelled structure would score high similarity without package guard
    s_cross = _make_step("s3", 1, "com.app2", ["a1", "a2", "a3", "a4", "a5"], tapped_rid="a1")
    # Normalize rid out to force cross-app similarity=1.0
    for st in (s1, s2, s_cross):
        for n in st.norm_nodes:
            n["resource-id"] = ""
    s1 = EvalStep(s1.session_id, 1, "i1", "p1", s1.norm_nodes, tokenize(s1.norm_nodes), s1.tapped)
    s2 = EvalStep(s2.session_id, 1, "i2", "p2", s2.norm_nodes, tokenize(s2.norm_nodes), s2.tapped)
    s_cross = EvalStep(s_cross.session_id, 1, "i3", "p3", s_cross.norm_nodes, tokenize(s_cross.norm_nodes), s_cross.tapped)

    from scripts.workflow_eval import LabeledPair
    pairs = [
        LabeledPair(1, "same", s1, s2, 1.0, 1.0),
        LabeledPair(0, "cross_app", s1, s_cross, 0.0, 0.90),
    ]
    roc = calibrate_tau(pairs)
    assert roc["selected_tau"] == 0.50
    assert roc["false_match_same_app"] == 0
    assert roc["false_match_cross_app_matcher"] == 0
    assert roc["cross_app_sim_ge_selected_tau"] == 1


def test_multi_edge_resolution_falls_back_when_multiple_targets_resolve():
    step_live = _make_step("sb", 1, "com.app", ["a1", "a2", "a3", "a4", "a5"], tapped_rid="a1")
    edge_1 = TappedElement("click", ("rid", "com.app:id/a1"), "com.app:id/a1", "android.widget.Button", "lbl_a1", (50, 100, 950, 190), (5, 1), False)
    edge_2 = TappedElement("click", ("rid", "com.app:id/a2"), "com.app:id/a2", "android.widget.Button", "lbl_a2", (50, 210, 950, 300), (5, 2), False)

    # Identical duplicate edges resolve uniquely
    node, edge, status = resolve_node_edges([edge_1, edge_1], step_live.norm_nodes)
    assert status == "ok" and node is not None and edge == edge_1

    # Two distinct resolvable targets trigger multi_edge fallback (Mục 3.5)
    node_m, edge_m, status_m = resolve_node_edges([edge_1, edge_2], step_live.norm_nodes)
    assert status_m == "multi_edge"
    assert node_m is None and edge_m is None

    # End-to-end simulation also records multi_edge fallback instead of picking the first edge
    sa_1 = _make_step("sa", 1, "com.app", ["a1", "a2", "a3", "a4", "a5"], tapped_rid="a1")
    sa_2 = _make_step("sa", 2, "com.app", ["a1", "a2", "a3", "a4", "a5"], tapped_rid="a2")
    sb_target = _make_step("sb", 1, "com.app", ["a1", "a2", "a3", "a4", "a5"], tapped_rid="a3", tapped_text="Đặt đơn")
    sim = simulate_test_workflows({"sa": [sa_1, sa_2], "sb": [sb_target]}, tau=0.80, delta=0.10)
    assert sim["proposed_steps"] == 0
    assert sim["fallback_reasons"].get("multi_edge") == 1


def test_oversized_root_container_rejected_in_extraction_and_resolution():
    raw = [
        {
            "class": "android.widget.FrameLayout",
            "package": "com.app",
            "resource-id": "com.app:id/root_container",
            "bounds": "[0,100][1000,970]",
            "clickable": "false",
            "visible-to-user": "true",
        },
        {
            "class": "android.widget.Button",
            "package": "com.app",
            "resource-id": "com.app:id/small_btn",
            "bounds": "[100,800][400,880]",
            "clickable": "true",
            "visible-to-user": "true",
            "text": "Continue",
        },
    ]
    norm = normalize(raw, width=1000, height=1000)
    # Coordinate tap outside small_btn but inside 87% root_container must NOT bind to root_container
    tap_on_canvas = json.dumps({"action": "tap", "coordinate_space": "normalized", "normalized_coordinates": [800, 840]})
    assert extract_tapped_element(tap_on_canvas, norm, 1000, 1000) is None

    # Resolving an oversized container resource-id directly is also blocked
    root_edge = TappedElement("click", ("rid", "com.app:id/root_container"), "com.app:id/root_container", "android.widget.FrameLayout", "", (0, 100, 1000, 970), (5, 5), False)
    node, status = _resolve_target_on_screen(root_edge, norm)
    assert node is None
    assert status == "oversized_container"


def test_simulation_counts_proposals_on_non_tap_and_unknown_target_steps():
    sa = _make_step("sa", 1, "com.app", ["a1", "a2", "a3", "a4", "a5"], tapped_rid="a1")

    # Screen matches sa, but AI actually executes swipe, unresolvable click, and matching tap a1
    sb_base = _make_step("sb", 1, "com.app", ["a1", "a2", "a3", "a4", "a5"], tapped_rid="a1")
    sb_swipe = EvalStep(
        "sb", 1, "i1", "p1", sb_base.norm_nodes, sb_base.fingerprint, None, action_name="swipe"
    )
    sb_unknown_click = EvalStep(
        "sb", 2, "i2", "p2", sb_base.norm_nodes, sb_base.fingerprint, None, action_name="click"
    )
    sb_match = EvalStep(
        "sb", 3, "i3", "p3", sb_base.norm_nodes, sb_base.fingerprint, sb_base.tapped, action_name="click"
    )

    sim = simulate_test_workflows({"sa": [sa], "sb": [sb_swipe, sb_unknown_click, sb_match]}, tau=0.80, delta=0.10)
    # Two-way simulation: sa->sb evaluates 3 steps (1 swipe, 1 unknown click, 1 tap) and sb->sa evaluates 1 tap step
    assert sim["total_target_steps"] == 4
    assert sim["total_tap_steps"] == 2
    assert sim["unknown_target_steps"] == 1
    assert sim["proposed_steps"] == 4
    assert sim["proposed_on_tap_steps"] == 2
    assert sim["unknown_target_proposals"] == 1
    assert sim["correct_proposals"] == 2
    assert sim["precision"] == 0.5
    assert sim["precision_on_tap_steps"] == 1.0
    assert sim["error_breakdown"]["ai_other_action:swipe"] == 1
    assert sim["error_breakdown"]["unknown_target"] == 1


def test_simulation_compares_click_sequence_against_first_element():
    sa = _make_step("sa", 1, "com.app", ["a1", "a2", "a3", "a4", "a5"], tapped_rid="a1")
    sb_base = _make_step("sb", 1, "com.app", ["a1", "a2", "a3", "a4", "a5"], tapped_rid="a1")

    cs_matching = json.dumps(
        {
            "action": "click_sequence",
            "coordinate_space": "normalized",
            "coordinates": [[500, 145], [500, 255]],
            "target_descriptions": ["lbl_a1", "lbl_a2"],
        }
    )
    seq_first = extract_sequence_first_element(cs_matching, sb_base.norm_nodes, 1000, 1000)
    assert seq_first is not None
    assert seq_first.resource_id == "com.app:id/a1"

    sb_cs_ok = EvalStep(
        "sb",
        1,
        "i1",
        "p1",
        sb_base.norm_nodes,
        sb_base.fingerprint,
        None,
        action_name="click_sequence",
        sequence_first_tapped=seq_first,
    )
    sb_cs_unresolvable = EvalStep(
        "sb",
        2,
        "i2",
        "p2",
        sb_base.norm_nodes,
        sb_base.fingerprint,
        None,
        action_name="click_sequence",
        sequence_first_tapped=None,
    )

    sim = simulate_test_workflows({"sa": [sa], "sb": [sb_cs_ok, sb_cs_unresolvable]}, tau=0.80, delta=0.10)
    assert sim["total_target_steps"] == 2
    assert sim["proposed_steps"] == 2
    assert sim["correct_proposals"] == 1
    assert sim["precision"] == 0.5
    assert sim["error_breakdown"]["ai_other_action:click_sequence"] == 1
