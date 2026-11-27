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

"""Weighted Jaccard screen matching and 5-condition acceptance gate (Mục 2.3)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from artemis.workflow.fingerprint import ScreenFingerprint

DEFAULT_TAU = 0.80
DEFAULT_DELTA = 0.10
MIN_APP_TOKENS = 5
# A sparse screen (a search page: back, field, clear) is still matchable when
# its few app nodes carry resource ids; WebView / canvas trees carry none.
MIN_APP_TOKENS_WITH_IDS = 3
MIN_RESOURCE_IDS_SPARSE = 2


def is_matchable(fp: ScreenFingerprint) -> bool:
    """Condition (5) of Mục 2.3, relaxed for sparse but id-rich native screens."""
    if not fp.package:
        return False
    if fp.app_token_count >= MIN_APP_TOKENS:
        return True
    return (
        fp.app_token_count >= MIN_APP_TOKENS_WITH_IDS
        and len(fp.resource_ids) >= MIN_RESOURCE_IDS_SPARSE
    )

WEIGHT_RESOURCE_ID = 3
WEIGHT_STATIC_TEXT = 2
WEIGHT_DEFAULT = 1


@dataclass(frozen=True)
class MatchResult:
    """Outcome of matching a live screen fingerprint against workflow node candidates."""

    matched: bool
    node_id: str | None
    score: float
    second_score: float
    margin: float
    reason: str


def token_weight(token: tuple[Any, ...]) -> int:
    """Returns token weight: resource-id=3, static text=2, structural=1."""
    if not token:
        return WEIGHT_DEFAULT
    if token[0] == "text":
        return WEIGHT_STATIC_TEXT
    if token[0] in ("node", "scroll_child") and len(token) >= 3 and token[2]:
        return WEIGHT_RESOURCE_ID
    return WEIGHT_DEFAULT


def _as_counter(fp: Counter[tuple[Any, ...]] | ScreenFingerprint) -> Counter[tuple[Any, ...]]:
    return fp.tokens if isinstance(fp, ScreenFingerprint) else fp


def weighted_jaccard(
    a: Counter[tuple[Any, ...]] | ScreenFingerprint,
    b: Counter[tuple[Any, ...]] | ScreenFingerprint,
) -> float:
    """Computes weighted Jaccard similarity over two token multisets."""
    ca = _as_counter(a)
    cb = _as_counter(b)
    if not ca or not cb:
        return 0.0

    intersection = 0
    union = 0
    for token in ca.keys() | cb.keys():
        va = ca.get(token, 0)
        vb = cb.get(token, 0)
        w = token_weight(token)
        if va and vb:
            intersection += min(va, vb) * w
        union += max(va, vb) * w
    return intersection / union if union > 0 else 0.0


def match_screen(
    query: ScreenFingerprint,
    candidates: Mapping[str, Sequence[ScreenFingerprint]]
    | Sequence[tuple[str, Sequence[ScreenFingerprint]]],
    *,
    tau: float = DEFAULT_TAU,
    delta: float = DEFAULT_DELTA,
    min_app_tokens: int = MIN_APP_TOKENS,
    target_resolver: Callable[[str], bool] | None = None,
) -> MatchResult:
    """Matches ``query`` against candidate nodes using all 5 conditions of Mục 2.3.

    Conditions checked simultaneously:
    (1) Foreground package matches.
    (2) Best similarity S1 >= ``tau``.
    (3) Margin S1 - S2 >= ``delta`` against the runner-up node.
    (4) Optional ``target_resolver(node_id)`` confirms unique, unoccluded target.
    (5) Query screen has >= ``min_app_tokens`` app tokens (rejects sparse trees).
    """
    if not query.package or query.app_token_count < min_app_tokens:
        return MatchResult(False, None, 0.0, 0.0, 0.0, "poor_tree")

    items = list(candidates.items()) if isinstance(candidates, Mapping) else list(candidates)
    if not items:
        return MatchResult(False, None, 0.0, 0.0, 0.0, "no_candidates")

    node_scores: list[tuple[str, float]] = []
    for node_id, fps in items:
        same_pkg_scores = [
            weighted_jaccard(query, fp) for fp in fps if fp.package == query.package
        ]
        if same_pkg_scores:
            node_scores.append((node_id, max(same_pkg_scores)))

    if not node_scores:
        return MatchResult(False, None, 0.0, 0.0, 0.0, "no_package_match")

    node_scores.sort(key=lambda pair: pair[1], reverse=True)
    best_id, best_score = node_scores[0]
    second_score = node_scores[1][1] if len(node_scores) > 1 else 0.0
    margin = best_score - second_score

    if best_score < tau:
        return MatchResult(False, None, best_score, second_score, margin, "below_tau")
    if margin < delta:
        return MatchResult(False, None, best_score, second_score, margin, "ambiguous_margin")
    if target_resolver is not None and not target_resolver(best_id):
        return MatchResult(False, None, best_score, second_score, margin, "target_unresolved")

    return MatchResult(True, best_id, best_score, second_score, margin, "matched")
