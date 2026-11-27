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

"""Shared UI hierarchy normalization and structural fingerprinting (M0)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
from typing import Any, Iterable

from artemis.utils.screen_diff import flatten_hierarchy, robust_parse_bounds

SYSTEMUI_PACKAGE = "com.android.systemui"
# On a normalized 0..1000 x 0..1000 canvas (area = 1_000_000), 0.1% is 1000.
MIN_AREA_NORM = 1000
_CONTAINMENT_TOLERANCE = 5


@dataclass(frozen=True)
class ScreenFingerprint:
    """Structural multiset fingerprint of a single UI screen."""

    package: str
    tokens: Counter[tuple[Any, ...]]
    app_token_count: int
    resource_ids: frozenset[str]
    node_count: int


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return default


def _extract_raw_bounds(node: dict[str, Any]) -> tuple[int, int, int, int] | None:
    parsed = robust_parse_bounds(node.get("bounds"))
    if parsed is not None:
        return parsed
    pb = node.get("parsed_bounds")
    if isinstance(pb, dict):
        try:
            return (int(pb["left"]), int(pb["top"]), int(pb["right"]), int(pb["bottom"]))
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _annotate_nested_scrollable(raw: Any) -> Any:
    """Marks descendants of scrollable nodes before flattening nested trees."""

    def _walk(item: Any, in_scrollable: bool) -> Any:
        if isinstance(item, list):
            return [_walk(child, in_scrollable) for child in item]
        if isinstance(item, dict):
            copy = dict(item)
            if in_scrollable:
                copy["_in_scrollable"] = True
            self_scrollable = _to_bool(copy.get("scrollable"), False)
            child_flag = in_scrollable or self_scrollable
            for key in ("children", "child"):
                if key in copy and copy[key]:
                    copy[key] = _walk(copy[key], child_flag)
            return copy
        return item

    if isinstance(raw, dict):
        return _walk(raw, False)
    if isinstance(raw, list) and any(
        isinstance(el, dict) and (el.get("children") or el.get("child")) for el in raw
    ):
        return _walk(raw, False)
    return raw


def _infer_dimensions(
    raw_bounds_list: list[tuple[int, int, int, int] | None],
    width: int | None,
    height: int | None,
) -> tuple[int, int]:
    if width and height and width > 0 and height > 0:
        return int(width), int(height)
    max_r = 0
    max_b = 0
    for box in raw_bounds_list:
        if box is not None:
            max_r = max(max_r, box[2])
            max_b = max(max_b, box[3])
    # If coordinates already sit within 0..1000 and no explicit screen size is given,
    # treat them as already normalized; otherwise scale against the observed bounds.
    inferred_w = width if (width and width > 0) else (max_r if max_r > 1000 or max_b > 1000 else 1000)
    inferred_h = height if (height and height > 0) else (max_b if max_r > 1000 or max_b > 1000 else 1000)
    return max(1, int(inferred_w)), max(1, int(inferred_h))


def _clamp_1000(val: float) -> int:
    return max(0, min(1000, int(round(val))))


def _scale_bounds(
    box: tuple[int, int, int, int] | None, width: int, height: int
) -> list[int]:
    if box is None:
        return [0, 0, 0, 0]
    left, top, right, bottom = box
    return [
        _clamp_1000(left * 1000.0 / width),
        _clamp_1000(top * 1000.0 / height),
        _clamp_1000(right * 1000.0 / width),
        _clamp_1000(bottom * 1000.0 / height),
    ]


def _mark_flat_scrollable_children(nodes: list[dict[str, Any]]) -> None:
    """Infers scrollable-container membership for already-flattened dumps.

    In DFS pre-order accessibility dumps (such as ``images.ui_tree``), a scrollable
    container precedes its descendants and its normalized bounds enclose them.
    """
    scroll_boxes: list[tuple[int, int, int, int]] = []
    for node in nodes:
        l, t, r, b = node["bounds"]
        if r <= l or b <= t:
            continue
        if not node["in_scrollable"] and scroll_boxes:
            cx = (l + r) // 2
            cy = (t + b) // 2
            for sl, st, sr, sb in scroll_boxes:
                if (
                    sl <= cx <= sr
                    and st <= cy <= sb
                    and l >= sl - _CONTAINMENT_TOLERANCE
                    and t >= st - _CONTAINMENT_TOLERANCE
                    and r <= sr + _CONTAINMENT_TOLERANCE
                    and b <= sb + _CONTAINMENT_TOLERANCE
                ):
                    node["in_scrollable"] = True
                    break
        if node["scrollable"] and (r - l) * (b - t) >= MIN_AREA_NORM:
            scroll_boxes.append((l, t, r, b))


def normalize(
    raw: Any,
    width: int | None = None,
    height: int | None = None,
) -> list[dict[str, Any]]:
    """Normalizes a raw nested or flat UI hierarchy into canonical 0-1000 node dicts.

    Shared single-entry normalizer for both offline traces (``images.ui_tree``) and
    runtime ``ObserveResult.raw_elements``.
    """
    if not raw:
        return []

    prepared = _annotate_nested_scrollable(raw)
    flat = flatten_hierarchy(prepared)
    if not flat:
        return []

    raw_boxes = [_extract_raw_bounds(node) for node in flat]
    eff_w, eff_h = _infer_dimensions(raw_boxes, width, height)

    normalized: list[dict[str, Any]] = []
    for node, box in zip(flat, raw_boxes):
        vis_raw = node.get("visible-to-user") if "visible-to-user" in node else node.get("visible_to_user")
        rid = str(node.get("resource-id") or node.get("resource_id") or "").strip()
        text = str(node.get("text") or "").strip()
        desc = str(node.get("content-desc") or node.get("content_description") or "").strip()
        normalized.append(
            {
                "resource-id": rid,
                "class": str(node.get("class") or "").strip(),
                "bounds": _scale_bounds(box, eff_w, eff_h),
                "clickable": _to_bool(node.get("clickable"), False),
                "scrollable": _to_bool(node.get("scrollable"), False),
                "focused": _to_bool(node.get("focused"), False),
                "checkable": _to_bool(node.get("checkable"), False),
                "checked": _to_bool(node.get("checked"), False),
                "selected": _to_bool(node.get("selected"), False),
                "enabled": _to_bool(node.get("enabled"), True),
                "visible-to-user": _to_bool(vis_raw, True),
                "package": str(node.get("package") or "").strip(),
                "editable": _to_bool(node.get("editable"), False),
                "text": text,
                "content-desc": desc,
                "hint": str(node.get("hint") or node.get("hint_text") or node.get("hintText") or "").strip(),
                "in_scrollable": bool(node.get("_in_scrollable", False)),
            }
        )

    _mark_flat_scrollable_children(normalized)
    return normalized


def foreground_package(normalized: list[dict[str, Any]]) -> str:
    """Infers the foreground app package without hardcoding IME or launcher names.

    Selects the non-systemui package owning the largest visible node area (breaking
    ties by node count), which naturally prefers the full-screen app over overlays
    like soft keyboards.
    """
    max_area: dict[str, int] = {}
    counts: dict[str, int] = {}
    for visible_only in (True, False):
        for node in normalized:
            if visible_only and not node.get("visible-to-user", True):
                continue
            pkg = str(node.get("package") or "").strip()
            if not pkg or pkg == SYSTEMUI_PACKAGE:
                continue
            l, t, r, b = node.get("bounds") or (0, 0, 0, 0)
            area = max(0, r - l) * max(0, b - t)
            max_area[pkg] = max(max_area.get(pkg, 0), area)
            counts[pkg] = counts.get(pkg, 0) + 1
        if max_area:
            break
    if not max_area:
        return ""
    return max(max_area, key=lambda pkg: (max_area[pkg], counts[pkg]))


def node_role(node: dict[str, Any]) -> str:
    """Maps a normalized node's capabilities to a canonical interaction role."""
    cls = str(node.get("class") or "")
    if node.get("editable") or "EditText" in cls or "AutoCompleteTextView" in cls:
        return "editable"
    if node.get("checkable"):
        return "checkable"
    if node.get("scrollable"):
        return "scrollable"
    if node.get("clickable"):
        return "clickable"
    return "static"


def size_bucket(span: int) -> int:
    """Coarse size bucket on a 0-1000 axis to tolerate minor label shifts."""
    if span < 50:
        return 0
    if span < 150:
        return 1
    if span < 350:
        return 2
    if span < 700:
        return 3
    return 4


def _static_text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def tokenize(
    normalized: list[dict[str, Any]],
    static_texts: Iterable[str] | None = None,
    *,
    foreground_only: bool = False,
) -> ScreenFingerprint:
    """Converts normalized nodes into a structural multiset fingerprint (Mục 2.2).

    ``foreground_only`` keeps only the foreground app's nodes: other windows
    (the soft keyboard, an overlay) change independently of the app screen
    and would otherwise dominate a sparse screen's fingerprint.
    """
    fg_pkg = foreground_package(normalized)
    static_set = {str(s).strip() for s in static_texts if str(s).strip()} if static_texts else set()

    tokens: Counter[tuple[Any, ...]] = Counter()
    seen_scroll_triples: set[tuple[str, str, str]] = set()
    resource_ids: set[str] = set()
    app_token_count = 0
    kept_nodes = 0

    for node in normalized:
        pkg = node.get("package") or ""
        if pkg == SYSTEMUI_PACKAGE:
            continue
        if foreground_only and fg_pkg and pkg != fg_pkg:
            continue
        if not node.get("visible-to-user", True):
            continue
        l, t, r, b = node.get("bounds") or (0, 0, 0, 0)
        if r <= l or b <= t:
            continue
        if (r - l) * (b - t) < MIN_AREA_NORM:
            continue

        kept_nodes += 1
        cls = node.get("class") or ""
        rid = node.get("resource-id") or ""
        role = node_role(node)
        if rid:
            resource_ids.add(rid)

        emitted: tuple[Any, ...] | None = None
        if node.get("in_scrollable") and not node.get("scrollable"):
            triple = (cls, rid, role)
            if triple not in seen_scroll_triples:
                seen_scroll_triples.add(triple)
                emitted = ("scroll_child", cls, rid, role)
        else:
            cx = (l + r) // 2
            cy = (t + b) // 2
            grid = (
                min(9, max(0, cx // 100)),
                min(9, max(0, cy // 100)),
                size_bucket(r - l),
                size_bucket(b - t),
            )
            emitted = ("node", cls, rid, role, grid)

        if emitted is not None:
            tokens[emitted] += 1
            if fg_pkg and pkg == fg_pkg:
                app_token_count += 1

        if static_set:
            for candidate_text in (node.get("text") or "", node.get("content-desc") or ""):
                if candidate_text and candidate_text in static_set:
                    tokens[("text", _static_text_hash(candidate_text))] += 1
                    if fg_pkg and pkg == fg_pkg:
                        app_token_count += 1

    return ScreenFingerprint(
        package=fg_pkg,
        tokens=tokens,
        app_token_count=app_token_count,
        resource_ids=frozenset(resource_ids),
        node_count=kept_nodes,
    )


def fingerprint_from_raw(
    raw: Any,
    width: int | None = None,
    height: int | None = None,
    static_texts: Iterable[str] | None = None,
) -> ScreenFingerprint:
    """Convenience helper: normalize raw UI hierarchy and tokenize in one call."""
    return tokenize(normalize(raw, width=width, height=height), static_texts=static_texts)


def _app_node_role(node: dict[str, Any]) -> str:
    """App-workflow role including active selection/checked states (W3)."""
    base = node_role(node)
    flags: list[str] = []
    if node.get("selected"):
        flags.append("selected")
    if node.get("checked"):
        flags.append("checked")
    return f"{base}:{'+'.join(flags)}" if flags else base


def app_fingerprint(normalized: list[dict[str, Any]]) -> ScreenFingerprint:
    """Workflow fingerprint of a screen's *layout*, robust to its data.

    - Only the foreground app's nodes: the keyboard or an overlay window
      changes independently of the app screen.
    - Presence, not counts: how many results / rows / cards a list shows
      depends on the data (a short result list does not even scroll).
    - A node with a resource id is named by it (no position: a keyboard
      resizing the window moves it across grid cells).
    - Unnamed nodes repeated down a column (rows) collapse into one token;
      other unnamed nodes keep their grid cell.
    """
    fg = foreground_package(normalized)
    tokens: Counter[tuple[Any, ...]] = Counter()
    resource_ids: set[str] = set()
    columns: dict[tuple[Any, ...], set[int]] = {}
    kept = 0
    for node in normalized:
        if not fg or node.get("package") != fg or not node.get("visible-to-user", True):
            continue
        l, t, r, b = node.get("bounds") or (0, 0, 0, 0)
        if r <= l or b <= t or (r - l) * (b - t) < MIN_AREA_NORM:
            continue
        kept += 1
        cls = node.get("class") or ""
        rid = node.get("resource-id") or ""
        role = _app_node_role(node)
        if rid:
            resource_ids.add(rid)
            tokens[("node", cls, rid, role)] = 1
            continue
        cx, cy = (l + r) // 2, (t + b) // 2
        key = (cls, role, min(9, max(0, cx // 100)), size_bucket(r - l))
        columns.setdefault(key, set()).add(min(9, max(0, cy // 100)))
    for (cls, role, gx, _w), rows in columns.items():
        if len(rows) >= 2:
            tokens[("rep", cls, role, gx)] = 1
        else:
            tokens[("node", cls, "", role, (gx, next(iter(rows))))] = 1
    return ScreenFingerprint(
        package=fg,
        tokens=tokens,
        app_token_count=kept,
        resource_ids=frozenset(resource_ids),
        node_count=kept,
    )
