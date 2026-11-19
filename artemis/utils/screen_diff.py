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

"""Screen difference detection and text matching utilities for Artemis.

App-agnostic algorithms for:
- Detecting screen visual and hierarchy changes with noise masking (clock, status bar, input focus).
- Text matching with Visibility Guard (bounds, clipping) and multi-language / accent support.
"""

from __future__ import annotations

import io
import re
from typing import Any
import unicodedata

from PIL import Image, ImageChops, ImageDraw

from artemis.utils.visualization import parse_bounds

CLOCK_PATTERN = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?(?:\.\d)?$")

COMMITMENT_PATTERN = re.compile(
    r"\b(place\s+order|đặt\s+(?:đơn|hàng)|pay\s+now|thanh\s+toán|confirm\s+purchase|xác\s+nhận\s+mua)\b",
    re.IGNORECASE,
)


def flatten_hierarchy(elements: Any) -> list[dict[str, Any]]:
    """Recursively flattens nested UI element hierarchies (e.g. tree with 'children').

    Guarantees downstream functions (wait_for_text, compare_xml_hierarchy,
    find_matching_text_elements) always receive a flat list of leaf and container nodes.
    """
    if not elements:
        return []
    flat: list[dict[str, Any]] = []

    def _walk(item: Any):
        if isinstance(item, list):
            for child in item:
                _walk(child)
        elif isinstance(item, dict):
            item_copy = dict(item)
            children = item_copy.pop("children", None) or item_copy.pop("child", None)
            flat.append(item_copy)
            if children:
                _walk(children)

    _walk(elements)
    return flat


def unaccent(s: str) -> str:
    """Removes combining diacritical marks and normalizes Vietnamese d/đ."""
    if not s:
        return ""
    normalized = unicodedata.normalize("NFD", s)
    stripped = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return stripped.replace("đ", "d").replace("Đ", "D").lower()


def robust_parse_bounds(bounds_val: Any) -> tuple[int, int, int, int] | None:
    """Robustly parses bounds from list, tuple, or various string formats."""
    if not bounds_val:
        return None
    if isinstance(bounds_val, (list, tuple)) and len(bounds_val) == 4:
        try:
            return (int(bounds_val[0]), int(bounds_val[1]), int(bounds_val[2]), int(bounds_val[3]))
        except (ValueError, TypeError):
            return None
    if isinstance(bounds_val, str):
        # First try visualization.parse_bounds
        res = parse_bounds(bounds_val)
        if res:
            return res
        # Otherwise find all integers
        nums = re.findall(r"-?\d+", bounds_val)
        if len(nums) == 4:
            try:
                return (int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3]))
            except (ValueError, TypeError):
                return None
    return None


def is_clock_text(s: str) -> bool:
    """Checks whether a string represents a clock, countdown, or timer."""
    if not s:
        return False
    return bool(CLOCK_PATTERN.match(s.strip()))


#: Largest element (fraction of the screen) mask_noise may black out. Clocks and
#: text inputs are small; anything bigger is content, not noise.
MAX_MASK_AREA_FRACTION = 0.15


def _is_text_input(el: dict[str, Any]) -> bool:
    """A field with a blinking caret: EditText-like class, or flagged editable/password."""
    cls = str(el.get("class") or "")
    if "EditText" in cls or "AutoCompleteTextView" in cls or "SearchView" in cls:
        return True
    return any(el.get(k) in (True, "true", "True", 1) for k in ("editable", "password"))


def mask_noise(
    img: Image.Image,
    elements: list[dict[str, Any]] | None,
    width: int,
    height: int,
) -> Image.Image:
    """Masks noise regions: Android status bar, clock text elements, and focused inputs."""
    img_copy = img.copy()
    draw = ImageDraw.Draw(img_copy)

    # 1. Mask top status bar (system clock and notification icons) ~4% of screen
    status_bar_height = max(1, int(height * 0.04))
    draw.rectangle([0, 0, width, status_bar_height], fill=0)

    if not elements:
        return img_copy

    # 2. Mask XML elements with clock patterns and focused text-input cursors.
    # Only small elements are ever masked: a focused container (Chrome's WebView
    # is "focused" and spans the whole page) would otherwise black out the very
    # content being watched, and no change could ever be detected.
    max_mask_area = MAX_MASK_AREA_FRACTION * max(1, width) * max(1, height)
    for el in elements:
        text = str(el.get("text") or "").strip()
        desc = str(el.get("content-desc") or el.get("content_description") or "").strip()
        is_focused_input = bool(el.get("focused") in (True, "true", "True", 1)) and _is_text_input(el)

        if is_clock_text(text) or is_clock_text(desc) or is_focused_input:
            bounds = robust_parse_bounds(el.get("bounds"))
            if bounds and len(bounds) == 4:
                l, t, r, b = bounds
                if (r - l) * (b - t) > max_mask_area:
                    continue
                # Clamp to image boundaries
                l = max(0, min(width - 1, int(l)))
                t = max(0, min(height - 1, int(t)))
                r = max(0, min(width, int(r)))
                b = max(0, min(height, int(b)))
                if r > l and b > t:
                    draw.rectangle([l, t, r, b], fill=0)

    return img_copy


def crop_region(
    img: Image.Image,
    region: list[int] | None,
    width: int,
    height: int,
) -> Image.Image:
    """Crops an image to a normalized [0, 1000] region if specified.

    Strictly uses [x1, y1, x2, y2] format matching element bounds [left, top, right, bottom].
    """
    if not region or len(region) < 4:
        return img

    v0, v1, v2, v3 = region[:4]
    x1 = int(v0 * width / 1000.0)
    y1 = int(v1 * height / 1000.0)
    x2 = int(v2 * width / 1000.0)
    y2 = int(v3 * height / 1000.0)

    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1

    x1 = max(0, min(width - 2, x1))
    y1 = max(0, min(height - 2, y1))
    x2 = max(x1 + 1, min(width, x2))
    y2 = max(y1 + 1, min(height, y2))

    return img.crop((x1, y1, x2, y2))


def compare_screen_pixels(
    img1: Image.Image,
    img2: Image.Image,
    region: list[int] | None = None,
    width: int = 1080,
    height: int = 2400,
    cell_threshold: int = 20,
    pixel_diff_threshold: int = 25,
) -> tuple[bool, int]:
    """Compares two images after noise masking and downsampling to 64x64 grid.

    Returns (is_changed, count_of_changed_cells).
    Default cell_threshold is 20 cells (~0.5% of 4096 cells) to prevent false triggers
    from blinking cursors or subtle background noise.
    """
    cropped1 = crop_region(img1, region, width, height)
    cropped2 = crop_region(img2, region, width, height)

    g1 = cropped1.convert("L").resize((64, 64), Image.Resampling.BILINEAR)
    g2 = cropped2.convert("L").resize((64, 64), Image.Resampling.BILINEAR)

    diff = ImageChops.difference(g1, g2)
    diff_data = diff.tobytes()
    changed_cells = sum(1 for val in diff_data if val > pixel_diff_threshold)

    return (changed_cells >= cell_threshold), changed_cells


def compare_xml_hierarchy(
    elements1: list[dict[str, Any]] | None,
    elements2: list[dict[str, Any]] | None,
    region: list[int] | None = None,
    width: int = 1080,
    height: int = 2400,
) -> tuple[bool, str]:
    """Compares two UI element lists, ignoring noise (clock text).

    When region is provided, only elements whose bounds or center intersect with
    region=[x1, y1, x2, y2] (0-1000 scale) are compared.
    Returns (is_changed, brief_diff_summary).
    """
    def _element_intersects_region(
        el: dict[str, Any], rx1: float, ry1: float, rx2: float, ry2: float
    ) -> bool:
        bounds = robust_parse_bounds(el.get("bounds"))
        if bounds and len(bounds) == 4:
            bx1, by1, bx2, by2 = bounds
            return max(bx1, rx1) < min(bx2, rx2) and max(by1, ry1) < min(by2, ry2)
        center = el.get("center")
        if center and len(center) == 2:
            cx, cy = center
            return rx1 <= cx <= rx2 and ry1 <= cy <= ry2
        return False

    def _extract_text_tokens(elements: list[dict[str, Any]] | None) -> list[str]:
        if not elements:
            return []
        tokens = []
        for el in elements:
            text = str(el.get("text") or "").strip()
            desc = str(el.get("content-desc") or el.get("content_description") or "").strip()
            t = text or desc
            if t and not is_clock_text(t):
                tokens.append(t)
        return tokens

    e1 = flatten_hierarchy(elements1)
    e2 = flatten_hierarchy(elements2)
    if region and len(region) >= 4:
        v0, v1, v2, v3 = region[:4]
        rx1 = v0 * width / 1000.0
        ry1 = v1 * height / 1000.0
        rx2 = v2 * width / 1000.0
        ry2 = v3 * height / 1000.0
        if rx2 < rx1:
            rx1, rx2 = rx2, rx1
        if ry2 < ry1:
            ry1, ry2 = ry2, ry1
        if e1:
            e1 = [el for el in e1 if _element_intersects_region(el, rx1, ry1, rx2, ry2)]
        if e2:
            e2 = [el for el in e2 if _element_intersects_region(el, rx1, ry1, rx2, ry2)]

    tokens1 = _extract_text_tokens(e1)
    tokens2 = _extract_text_tokens(e2)

    if tokens1 == tokens2:
        return False, ""

    set1 = set(tokens1)
    set2 = set(tokens2)
    added = set2 - set1
    removed = set1 - set2

    diff_parts = []
    if added:
        diff_parts.append(f"added: {list(added)[:3]}")
    if removed:
        diff_parts.append(f"removed: {list(removed)[:3]}")

    summary = ", ".join(diff_parts) or "UI element hierarchy updated"
    return True, summary


def find_matching_text_elements(
    elements: list[dict[str, Any]] | None,
    targets: list[str] | str,
    width: int = 1080,
    height: int = 2400,
) -> list[dict[str, Any]]:
    """Finds visible UI elements matching target texts with Visibility Guard.

    Priority ranking:
    1. Exact match (case-insensitive)
    2. Starts with (case-insensitive)
    3. Contains substring (case-insensitive / unaccented)

    Returns a list of matching element dicts, sorted by match priority.
    """
    if not elements or not targets:
        return []

    elements = flatten_hierarchy(elements)

    target_list = [targets] if isinstance(targets, str) else list(targets)
    candidates: list[tuple[str, str]] = []
    for t in target_list:
        clean = str(t).strip().lower()
        if clean:
            candidates.append((clean, unaccent(clean)))

    if not candidates:
        return []

    matches: list[tuple[int, dict[str, Any], str]] = []

    for el in elements:
        bounds = robust_parse_bounds(el.get("bounds"))
        if not bounds or len(bounds) != 4:
            continue
        l, t, r, b = bounds
        # Visibility Guard: positive area and within viewport
        if r <= l or b <= t or r <= 0 or b <= 0 or l >= width or t >= height:
            continue

        # Check explicit visibility flags from Android accessibility / XML
        if el.get("visible-to-user") in (False, "false", "False") or el.get("displayed") in (False, "false", "False"):
            continue

        # Check minimum visible overlap with viewport: avoid matching elements peeking at edges
        visible_w = min(r, width) - max(l, 0)
        visible_h = min(b, height) - max(t, 0)
        if visible_w < 14 or visible_h < 14:
            continue
        if visible_h < (b - t) * 0.4:
            continue

        raw_text = str(el.get("text") or "").strip().lower()
        raw_desc = str(el.get("content-desc") or el.get("content_description") or "").strip().lower()
        raw_hint = str(el.get("hint") or "").strip().lower()

        unacc_text = unaccent(raw_text)
        unacc_desc = unaccent(raw_desc)
        unacc_hint = unaccent(raw_hint)

        best_score = 99
        matched_str = ""

        for cand, unacc_cand in candidates:
            for text_val, unacc_val in [
                (raw_text, unacc_text),
                (raw_desc, unacc_desc),
                (raw_hint, unacc_hint),
            ]:
                if not text_val:
                    continue
                # 1. Exact match
                if text_val == cand or unacc_val == unacc_cand:
                    if 1 < best_score:
                        best_score = 1
                        matched_str = cand
                    continue
                # 2. Starts with
                if text_val.startswith(cand) or unacc_val.startswith(unacc_cand):
                    if 2 < best_score:
                        best_score = 2
                        matched_str = cand
                    continue
                # 3. Substring
                if cand in text_val or unacc_cand in unacc_val:
                    if 3 < best_score:
                        best_score = 3
                        matched_str = cand

        if best_score < 99:
            el_copy = dict(el)
            el_copy["_match_score"] = best_score
            el_copy["_matched_target"] = matched_str
            el_copy["_parsed_bounds"] = [l, t, r, b]
            matches.append((best_score, el_copy, matched_str))

    matches.sort(key=lambda x: x[0])
    return [m[1] for m in matches]
