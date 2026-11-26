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

"""Unit tests for artemis.workflow.matcher (Mục 2.3)."""

from collections import Counter

import pytest

from artemis.workflow.fingerprint import ScreenFingerprint, fingerprint_from_raw
from artemis.workflow.matcher import (
    match_screen,
    token_weight,
    weighted_jaccard,
)


def _make_fp(
    pkg: str = "com.example.app",
    n_rid: int = 5,
    prefix: str = "btn",
    static: tuple[str, ...] = (),
) -> ScreenFingerprint:
    raw = [
        {
            "package": pkg,
            "class": "android.widget.Button",
            "resource-id": f"{pkg}:id/{prefix}_{i}",
            "bounds": f"[50,{100 + i * 110}][950,{190 + i * 110}]",
            "clickable": "true",
            "visible-to-user": "true",
            "text": static[i] if i < len(static) else "",
        }
        for i in range(n_rid)
    ]
    return fingerprint_from_raw(raw, width=1000, height=1000, static_texts=static or None)


def test_weighted_jaccard_uses_3_2_1_weights():
    tok_rid = ("node", "Button", "com.app:id/ok", "clickable", (5, 5, 2, 1))
    tok_txt = ("text", "abc123")
    tok_plain = ("node", "View", "", "static", (1, 1, 1, 1))
    assert token_weight(tok_rid) == 3
    assert token_weight(tok_txt) == 2
    assert token_weight(tok_plain) == 1

    a = Counter({tok_rid: 1, tok_txt: 1, tok_plain: 1})
    b = Counter({tok_rid: 1, tok_plain: 1})
    # Intersection = 3 + 1 = 4; Union = 3 + 2 + 1 = 6
    assert weighted_jaccard(a, b) == pytest.approx(4 / 6)


def test_match_screen_rejects_sparse_tree_under_5_app_tokens():
    sparse = _make_fp(n_rid=4)
    cand = {"n1": [_make_fp(n_rid=4)]}
    res = match_screen(sparse, cand)
    assert not res.matched
    assert res.reason == "poor_tree"


def test_match_screen_rejects_different_foreground_package():
    query = _make_fp(pkg="com.other.app", n_rid=6)
    cand = {"n1": [_make_fp(pkg="com.example.app", n_rid=6)]}
    res = match_screen(query, cand)
    assert not res.matched
    assert res.reason == "no_package_match"


def test_match_screen_rejects_below_tau_and_ambiguous_margin():
    q = _make_fp(n_rid=6, prefix="home")
    far = _make_fp(n_rid=6, prefix="cart")
    res_low = match_screen(q, {"n_cart": [far]}, tau=0.80)
    assert not res_low.matched
    assert res_low.reason == "below_tau"

    # Two nearly identical candidate nodes violate S1 - S2 >= delta
    res_ambig = match_screen(q, {"n1": [q], "n2": [q]}, tau=0.80, delta=0.10)
    assert not res_ambig.matched
    assert res_ambig.reason == "ambiguous_margin"


def test_match_screen_checks_target_resolver_and_accepts_clear_winner():
    q = _make_fp(n_rid=6, prefix="home")
    other = _make_fp(n_rid=6, prefix="settings")
    cands = {"n_home": [q], "n_settings": [other]}

    blocked = match_screen(q, cands, target_resolver=lambda nid: False)
    assert not blocked.matched
    assert blocked.reason == "target_unresolved"

    ok = match_screen(q, cands, target_resolver=lambda nid: nid == "n_home")
    assert ok.matched
    assert ok.node_id == "n_home"
    assert ok.score == pytest.approx(1.0)
    assert ok.margin == pytest.approx(1.0)
