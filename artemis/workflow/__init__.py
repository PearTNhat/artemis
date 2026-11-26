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

"""App-agnostic screen fingerprinting and matching for Flash app workflows."""

from artemis.workflow.fingerprint import (
    MIN_AREA_NORM,
    SYSTEMUI_PACKAGE,
    ScreenFingerprint,
    fingerprint_from_raw,
    foreground_package,
    node_role,
    normalize,
    size_bucket,
    tokenize,
)
from artemis.workflow.matcher import (
    DEFAULT_DELTA,
    DEFAULT_TAU,
    MIN_APP_TOKENS,
    WEIGHT_DEFAULT,
    WEIGHT_RESOURCE_ID,
    WEIGHT_STATIC_TEXT,
    MatchResult,
    match_screen,
    token_weight,
    weighted_jaccard,
)

__all__ = [
    "DEFAULT_DELTA",
    "DEFAULT_TAU",
    "MIN_APP_TOKENS",
    "MIN_AREA_NORM",
    "MatchResult",
    "SYSTEMUI_PACKAGE",
    "ScreenFingerprint",
    "WEIGHT_DEFAULT",
    "WEIGHT_RESOURCE_ID",
    "WEIGHT_STATIC_TEXT",
    "fingerprint_from_raw",
    "foreground_package",
    "match_screen",
    "node_role",
    "normalize",
    "size_bucket",
    "token_weight",
    "tokenize",
    "weighted_jaccard",
]
