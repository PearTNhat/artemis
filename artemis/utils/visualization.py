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

import difflib
import functools
import io
import os
import re
from typing import Any

from artemis.utils.coordinates import parse_swipe_parameters
from PIL import Image, ImageDraw, ImageFont
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


def draw_bounding_boxes(
    image_path: str, boxes: list[list[int]], labels: list[str], output_path: str
):
    """Draw bounding boxes and labels on an image.

    Args:
        image_path: Path to the original image.
        boxes: List of boxes, each box is [left, top, right, bottom].
        labels: List of labels corresponding to each box.
        output_path: Path to save the annotated image.
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image not found at {image_path}")

    img = Image.open(image_path)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    draw = ImageDraw.Draw(img)

    # Use a default font or try to load a system font
    try:
        font = ImageFont.load_default(size=45)
    except Exception:
        font = None

    for box, label in zip(boxes, labels):
        left, top, right, bottom = box

        # Draw rectangle
        draw.rectangle([left, top, right, bottom], outline="red", width=3)

        # Draw label with outline stroke for high contrast
        if font:
            draw.text(
                (left, top),
                label,
                fill="red",
                font=font,
                stroke_width=5,
                stroke_fill="white",
            )
        else:
            draw.text(
                (left, top),
                label,
                fill="red",
                stroke_width=5,
                stroke_fill="white",
            )
    img.save(output_path)


def draw_dots(
    image_path: str,
    points: list[list[int]],
    labels: list[str],
    output_path: str,
    radius: int = 10,
    color: str = "red",
):
    """Draw dots and labels on an image.

    Args:
        image_path: Path to the original image.
        points: List of points, each point is [x, y].
        labels: List of labels corresponding to each point.
        output_path: Path to save the annotated image.
        radius: Radius of the dot.
        color: Color of the indicator dot and text label.
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image not found at {image_path}")

    img = Image.open(image_path)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.load_default(size=45)
    except Exception:
        font = None

    # 1. Draw all background and foreground dots first
    for point in points:
        x, y = point

        # Draw larger white background dot (2x radius)
        white_radius = 2 * radius
        draw.ellipse(
            [
                x - white_radius,
                y - white_radius,
                x + white_radius,
                y + white_radius,
            ],
            fill="white",
            outline="white",
        )

        # Draw colored indicator dot on top
        draw.ellipse(
            [x - radius, y - radius, x + radius, y + radius],
            fill=color,
            outline=color,
        )

    # 2. Draw all text labels on top
    for point, label in zip(points, labels):
        x, y = point
        white_radius = 2 * radius

        # Draw label shifted to prevent overlap with white dot and outline with a white stroke
        if font:
            draw.text(
                (x + white_radius + 4, y - 22),
                label,
                fill=color,
                font=font,
                stroke_width=5,
                stroke_fill="white",
            )
        else:
            draw.text(
                (x + white_radius + 4, y - 22),
                label,
                fill=color,
                stroke_width=5,
                stroke_fill="white",
            )

    img.save(output_path)


def parse_bounds(bounds_str: str | None) -> tuple[int, int, int, int] | None:
    """Parses standard Android '[left,top][right,bottom]' bounds string."""
    if not bounds_str:
        return None
    match = re.match(r"\[(\-?\d+),(\-?\d+)\]\[(\-?\d+),(\-?\d+)\]", bounds_str)
    if match:
        return tuple(map(int, match.groups()))
    return None


def get_center_coordinates(left: int, top: int, right: int, bottom: int) -> tuple[int, int]:
    """Returns pixel center coordinates of a bounding box."""
    return (left + right) // 2, (top + bottom) // 2


def _is_truthy(val: Any) -> bool:
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1")
    return bool(val)


def _inject_mutual_occlusion_warnings(
    lines: list[str],
    items_bounds: list[tuple[int, tuple[int, int, int, int]]],
    elements: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Detect pairs of elements with >= 50% overlap and append a soft, restrained warning

    to the generated element description lines, and marks them as occluded in elements.
    Excludes pure parent-child enclosing bounds to prevent noise on standard
    nested containers.
    """
    if len(items_bounds) < 2:
        return lines

    warnings_map: dict[int, list[int]] = {}
    n = len(items_bounds)
    # Text read from pixels (a virtual OCR node) is drawn by whatever element
    # lies under it: overlapping that element is expected, never occlusion.
    virtual_ocr = {el.get("index") for el in (elements or []) if el.get("ocr_virtual")}

    for i in range(n):
        idx_i, b_i = items_bounds[i]
        l_i, t_i, r_i, bot_i = b_i
        area_i = max(1, (r_i - l_i) * (bot_i - t_i))

        for j in range(i + 1, n):
            idx_j, b_j = items_bounds[j]
            if idx_i in virtual_ocr or idx_j in virtual_ocr:
                continue
            l_j, t_j, r_j, bot_j = b_j
            area_j = max(1, (r_j - l_j) * (bot_j - t_j))

            # Calculate intersection
            inter_l = max(l_i, l_j)
            inter_t = max(t_i, t_j)
            inter_r = min(r_i, r_j)
            inter_bot = min(bot_i, bot_j)

            if inter_l >= inter_r or inter_t >= inter_bot:
                continue

            inter_area = (inter_r - inter_l) * (inter_bot - inter_t)
            ratio_i = inter_area / area_i
            ratio_j = inter_area / area_j

            # To be considered typical parent-child nesting (and NOT occlusion), one node must be inside another,
            # AND their centers must be closely aligned (i.e. concentric/enclosing layout).
            # If one node overlaps/sits inside another but their centers are significantly offset (e.g. a right-aligned Floating Action Button covering the right end of a left-aligned list item), it is mutual occlusion!
            center_i_x = (l_i + r_i) / 2.0
            center_i_y = (t_i + bot_i) / 2.0
            center_j_x = (l_j + r_j) / 2.0
            center_j_y = (t_j + bot_j) / 2.0
            center_dist = ((center_i_x - center_j_x) ** 2 + (center_i_y - center_j_y) ** 2) ** 0.5

            max_dim = max(r_i - l_i, r_j - l_j, bot_i - t_i, bot_j - t_j)
            is_concentric = center_dist < (max_dim * 0.20)

            is_i_inside_j = (
                (l_i >= l_j and t_i >= t_j and r_i <= r_j and bot_i <= bot_j)
                and (area_j > area_i * 2.0)
                and is_concentric
            )
            is_j_inside_i = (
                (l_j >= l_i and t_j >= t_i and r_j <= r_i and bot_j <= bot_i)
                and (area_i > area_j * 2.0)
                and is_concentric
            )
            if is_i_inside_j or is_j_inside_i:
                continue

            if ratio_i >= 0.50 or ratio_j >= 0.50:
                warnings_map.setdefault(idx_i, []).append(idx_j)
                warnings_map.setdefault(idx_j, []).append(idx_i)

    if not warnings_map:
        return lines

    if elements:
        for el in elements:
            el_idx = el.get("index")
            if el_idx in warnings_map:
                el["occluded"] = True
                el["occluded_by"] = warnings_map[el_idx]

    new_lines = []
    for line in lines:
        match = re.match(r"^\[(\d+)\]", line)
        if match:
            el_idx = int(match.group(1))
            if el_idx in warnings_map:
                overlaps = sorted(list(set(warnings_map[el_idx])))
                overlaps_str = " and ".join(f"[{o}]" for o in overlaps[:2])
                if len(overlaps) > 2:
                    overlaps_str += f" (+{len(overlaps) - 2} more)"
                line = f"{line} (WARNING: may overlap with {overlaps_str}, possible occlusion)"
        new_lines.append(line)
    return new_lines


def format_minimal_list_with_points(
    fused_xml: list[dict[str, Any]], width: int = 1080, height: int = 2400
) -> tuple[str, list[list[int]], list[str]]:
    """Formats fused XML hierarchy into a minimal list with sequence numbers,

    and returns physical coordinate points & labels for drawing.
    """
    lines = []
    points = []
    labels = []
    idx = 1
    added_items = []  # list of tuples: (text, cx, cy)
    items_bounds = []  # list of tuples: (idx, (left, top, right, bottom))

    def state_token(value: Any) -> str:
        token = str(value or "").strip().lower()
        return token if token in ("true", "false") else "?"

    def normalize_bounds(left: int, top: int, right: int, bottom: int) -> str:
        n_left = int(max(0, min(1000, left * 1000 / width)))
        n_top = int(max(0, min(1000, top * 1000 / height)))
        n_right = int(max(0, min(1000, right * 1000 / width)))
        n_bottom = int(max(0, min(1000, bottom * 1000 / height)))
        return f"[{n_left},{n_top}][{n_right},{n_bottom}]"

    def is_duplicate(text_val: str, cx: int, cy: int) -> bool:
        text_clean = text_val.strip()
        for ex_text, ex_cx, ex_cy in added_items:
            if ex_text == text_clean:
                dist = ((cx - ex_cx) ** 2 + (cy - ex_cy) ** 2) ** 0.5
                if dist < 8.0:
                    return True
        return False

    for node in fused_xml:
        text = node.get("text") or node.get("content-desc") or ""
        bounds_str = node.get("bounds")
        bounds = parse_bounds(bounds_str)

        ocr_elements = node.get("ocr_elements")
        registered = False

        if ocr_elements:
            for ocr in ocr_elements:
                ocr_bounds = parse_bounds(ocr.get("bounds"))
                if ocr_bounds:
                    left, top, right, bottom = ocr_bounds
                    if right <= left or bottom <= top or right <= 0 or bottom <= 0 or left >= width or top >= height:
                        continue
                    cx, cy = get_center_coordinates(left, top, right, bottom)
                    if is_duplicate(ocr["text"], cx, cy):
                        continue

                    norm_bounds = normalize_bounds(left, top, right, bottom)
                    lines.append(f"[{idx}] OCR Text: '{ocr['text']}' | Bounds: {norm_bounds}")
                    items_bounds.append((idx, (left, top, right, bottom)))

                    points.append([cx, cy])
                    labels.append(str(idx))
                    added_items.append((ocr["text"].strip(), cx, cy))
                    idx += 1
                    registered = True

        if not registered and text.strip() and bounds:
            left, top, right, bottom = bounds
            cx, cy = get_center_coordinates(left, top, right, bottom)
            if is_duplicate(text, cx, cy):
                continue

            norm_bounds = normalize_bounds(left, top, right, bottom)
            line = f"[{idx}] Text: '{text.strip()}' | Bounds: {norm_bounds}"
            checkable = str(node.get("checkable") or "").strip().lower() == "true"
            if checkable or node.get("checked") is not None:
                line += " | " + " ".join(
                    f"{a}:{state_token(node.get(a))}" for a in ("checked", "enabled")
                )
            elif str(node.get("enabled") or "").strip().lower() == "false":
                line += " | enabled:false"
            lines.append(line)
            items_bounds.append((idx, (left, top, right, bottom)))

            points.append([cx, cy])
            labels.append(str(idx))
            added_items.append((text.strip(), cx, cy))
            idx += 1

    lines = _inject_mutual_occlusion_warnings(lines, items_bounds)
    return "\n".join(lines), points, labels


#: Status-bar band, as a fraction of the screen height: system UI nodes lying
#: entirely inside it (clock, signal, battery, notification icons) are dropped
#: from the compact list. A heads-up notification or the shade is taller and stays.
_STATUS_BAR_FRACTION = 0.06
#: An unlabeled clickable larger than this share of the screen is a container
#: (card, banner, list row, backdrop), not an icon control. A 48dp icon button
#: is under 1% of a phone screen; 5% leaves room for wide pill buttons.
_ICON_MAX_AREA_FRACTION = 0.05
#: Inputs (search fields) span the screen width, so they get a larger cap.
_INPUT_MAX_AREA_FRACTION = 0.15
_ICON_MIN_SIDE_PX = 12
_MAX_ICON_LINES = 40
#: A touchable node drawn after (on top of) other content and covering at least
#: this share of the screen is a layer: bottom sheet, dialog, full-screen panel.
_OVERLAY_MIN_AREA_FRACTION = 0.30
#: Labeled nodes of the screen below that must lie under the layer before it
#: counts as covering anything (one stray label is not a covered screen).
_OVERLAY_MIN_COVERED_LABELS = 2
#: A node at least this much inside the layer is hidden by it.
_OVERLAY_HIDDEN_FRACTION = 0.8
#: An unlabeled control this flat (share of screen height) and at least twice as
#: wide as tall is a strip of a control cut off by an edge or a pinned bar.
_CUT_OFF_MAX_HEIGHT_FRACTION = 0.025
_CUT_OFF_EXEMPT_CLASSES = ("SeekBar", "Slider", "ProgressBar", "RatingBar")
#: OCR lines added to fill gaps of the tree, at most.
_MAX_GAP_OCR_LINES = 15
_GAP_OCR_MIN_SCORE = 0.5
_GAP_OCR_MARGIN_PX = 4
#: Bottom band below every listed element: at least this share of the screen
#: tall (a pinned action bar), ending above the gesture / navigation area.
_BOTTOM_BAND_MIN_FRACTION = 0.04
_BOTTOM_BAND_NAV_FRACTION = 0.03
#: Grey-level standard deviation under which the band is a blank background.
_BOTTOM_BAND_MIN_STDDEV = 6.0


def _node_label(node: dict[str, Any]) -> str:
    return str(node.get("text") or node.get("content-desc") or node.get("hint") or "").strip()


def find_overlay_hidden(
    nodes: list[dict[str, Any]], width: int, height: int
) -> tuple[set[int], list[tuple[int, int, int, int]]]:
    """Nodes of the flat (pre-order) list that a later layer hides.

    A layer is a touchable node covering at least ``_OVERLAY_MIN_AREA_FRACTION``
    of the screen that comes after (is drawn on top of) at least
    ``_OVERLAY_MIN_COVERED_LABELS`` labeled nodes lying inside it, and has labeled
    content of its own. In pre-order an earlier node inside a later one cannot be
    its descendant, so it belongs to the screen below. Returns the positions of
    the hidden nodes and the layers' bounds. App-agnostic: only geometry, order
    and the touchable flag are used.
    """
    screen = max(1, width * height)
    boxes = [parse_bounds(n.get("bounds")) if isinstance(n, dict) else None for n in nodes]
    hidden: set[int] = set()
    layers: list[tuple[int, int, int, int]] = []

    def inside_share(inner, outer) -> float:
        il, it, ir, ib = inner
        ol, ot, orr, ob = outer
        iw, ih = min(ir, orr) - max(il, ol), min(ib, ob) - max(it, ot)
        if iw <= 0 or ih <= 0:
            return 0.0
        return iw * ih / max(1, (ir - il) * (ib - it))

    for pos, node in enumerate(nodes):
        box = boxes[pos]
        if not box or not isinstance(node, dict):
            continue
        if not (_is_truthy(node.get("clickable")) or _is_truthy(node.get("long-clickable"))):
            continue
        l, t, r, b = box
        if (r - l) * (b - t) < screen * _OVERLAY_MIN_AREA_FRACTION:
            continue
        # Only the same app's own screen below: another window (keyboard, system
        # UI, a heads-up from another app) may be listed earlier yet drawn on top.
        package = node.get("package")
        below = [
            i for i in range(pos)
            if boxes[i]
            and nodes[i].get("package") == package
            and inside_share(boxes[i], box) >= _OVERLAY_HIDDEN_FRACTION
            # An earlier node enclosing the layer is its ancestor, not covered by it.
            and inside_share(box, boxes[i]) < 0.95
        ]
        covered_labels = sum(1 for i in below if _node_label(nodes[i]))
        if covered_labels < _OVERLAY_MIN_COVERED_LABELS:
            continue
        own_content = any(
            boxes[j] and _node_label(nodes[j]) and inside_share(boxes[j], box) >= _OVERLAY_HIDDEN_FRACTION
            for j in range(pos + 1, len(nodes))
        )
        if not own_content:
            continue
        hidden.update(below)
        layers.append(box)
    return hidden, layers


def _ocr_box(res: dict[str, Any]) -> tuple[int, int, int, int] | None:
    pts = res.get("position") or []
    if len(pts) < 2:
        return None
    xs, ys = [int(p["x"]) for p in pts], [int(p["y"]) for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _text_key(text: str) -> str:
    from artemis.utils.screen_diff import unaccent

    return re.sub(r"[^0-9a-z]", "", unaccent(str(text or "")).lower())


def verified_overlay_hidden(
    nodes: list[dict[str, Any]],
    ocr_results: list[dict[str, Any]],
    width: int,
    height: int,
) -> set[int]:
    """Positions of labeled nodes a layer on top really covers, checked against pixels.

    The tree's order alone is not reliable (some dumps list a page's own list
    container after its rows), so a candidate from :func:`find_overlay_hidden` is
    hidden only when OCR does not read its label where the tree puts it. When a
    sizable share of the candidates is still readable on screen, the "layer" is
    not covering anything and nothing is hidden. Without OCR nothing is hidden.
    """
    candidates, _layers = find_overlay_hidden(nodes, width, height)
    if not candidates or not ocr_results:
        return set()
    lines = [(box, _text_key(r.get("text"))) for r in ocr_results if (box := _ocr_box(r))]

    def readable(node: dict[str, Any]) -> bool:
        box = parse_bounds(node.get("bounds"))
        key = _text_key(_node_label(node))
        if not box or len(key) < 2:
            return True  # cannot be checked: treat as visible (never hide on doubt)
        l, t, r, b = box
        for (ol, ot, orr, ob), okey in lines:
            if ol >= r or orr <= l or ot >= b or ob <= t or not okey:
                continue
            if key in okey or okey in key:
                return True
            if difflib.SequenceMatcher(None, key, okey).ratio() >= 0.6:
                return True
        return False

    labeled = [i for i in candidates if _node_label(nodes[i])]
    if len(labeled) < _OVERLAY_MIN_COVERED_LABELS:
        return set()
    covered = [i for i in labeled if not readable(nodes[i])]
    if len(covered) < _OVERLAY_MIN_COVERED_LABELS or len(covered) < 0.7 * len(labeled):
        return set()
    # The layer is confirmed: what cannot be read (icons, one-character labels)
    # under it is covered too. Labels still readable on screen stay listed.
    uncheckable = {
        i for i in candidates
        if len(_text_key(_node_label(nodes[i]))) < 2
    }
    return set(covered) | uncheckable


def _required_markers(nodes: list[dict[str, Any]], width: int) -> tuple[set[int], set[int]]:
    """Labels marked required by an asterisk (the universal UI convention).

    Returns ``(required, stars)`` as sets of ``id(node)``: a label ending in "*"
    is required; a standalone "*" node is folded into the nearest label on its
    row just to its left (it then disappears from the list).
    """
    required: set[int] = set()
    stars: set[int] = set()
    labeled = [(n, parse_bounds(n.get("bounds"))) for n in nodes if _node_label(n)]
    for n, box in labeled:
        label = _node_label(n)
        if label == "*" and box:
            l, t, r, b = box
            best, best_gap = None, width * 0.06
            for m, mb in labeled:
                if m is n or not mb or _node_label(m) == "*":
                    continue
                overlap = min(b, mb[3]) - max(t, mb[1])
                gap = l - mb[2]
                if overlap > 0.5 * min(b - t, mb[3] - mb[1]) and -2 <= gap < best_gap:
                    best, best_gap = m, gap
            if best is not None:
                required.add(id(best))
                stars.add(id(n))
        elif label.endswith("*") and len(label) > 1:
            required.add(id(n))
    return required, stars


def _relation_hint(e: dict[str, Any], labeled: list[dict[str, Any]], width: int, height: int) -> str:
    """Structural relation of an unlabeled element to nearby text (no pixel numbers):
    contains "X" > same row as "X", left/right of it > above/below "X" > screen zone."""
    x1, y1, x2, y2 = e["bounds"]
    cx, cy = e["center"]
    zone_v = "top bar" if cy < 0.12 * height else "bottom bar" if cy > 0.88 * height else ""
    zone_h = "left" if cx < width / 3 else "right" if cx > 2 * width / 3 else "center"
    zone = f"{zone_v}, {zone_h}" if zone_v else ""

    def name(t):
        return '"' + str(t["text"])[:35].replace('"', "'").replace("\n", " ") + '"'

    def join(rel):
        return f"{rel}; {zone}" if zone else rel

    inside = [t for t in labeled if x1 <= t["center"][0] <= x2 and y1 <= t["center"][1] <= y2]
    if inside:
        return join("contains " + name(min(inside, key=lambda t: t["bounds"][1])))
    row = [t for t in labeled
           if min(y2, t["bounds"][3]) - max(y1, t["bounds"][1]) > 0.5 * min(y2 - y1, t["bounds"][3] - t["bounds"][1])]
    if row:
        t = min(row, key=lambda t: abs(t["center"][0] - cx))
        return join(f"same row as {name(t)}, {'right' if cx > t['center'][0] else 'left'} of it")
    reach = 1.2 * (x2 - x1)
    below = [t for t in labeled if t["bounds"][1] >= y2 - 5 and t["bounds"][1] - y2 < 0.12 * height
             and t["bounds"][0] - reach <= cx <= t["bounds"][2] + reach]
    if below:
        return join("above " + name(min(below, key=lambda t: (t["bounds"][1], abs(t["center"][0] - cx)))))
    above = [t for t in labeled if t["bounds"][3] <= y1 + 5 and y1 - t["bounds"][3] < 0.12 * height
             and t["bounds"][0] - reach <= cx <= t["bounds"][2] + reach]
    if above:
        return join("below " + name(min(above, key=lambda t: (-t["bounds"][3], abs(t["center"][0] - cx)))))
    return zone


def add_relation_hints(text: str, elements: list[dict[str, Any]], width: int, height: int) -> str:
    """Appends ``[relation]`` to unlabeled lines of a compact list (text-only turns)."""
    labeled = [e for e in elements if str(e.get("text") or "").strip() and e.get("bounds") and e.get("center")]
    hints = {}
    for e in elements:
        if str(e.get("text") or "").strip() or not e.get("bounds") or not e.get("center"):
            continue
        hint = _relation_hint(e, labeled, width, height)
        if hint:
            hints[e.get("index")] = hint
    out = []
    for line in text.splitlines():
        m = re.match(r"\[(\d+)\]", line)
        if m and int(m.group(1)) in hints:
            line += f" [{hints[int(m.group(1))]}]"
        out.append(line)
    return "\n".join(out)


def _is_cut_off_strip(element: dict[str, Any], height: int) -> bool:
    """An unlabeled control showing only a flat strip (rest hidden past an edge or bar)."""
    if any(k in str(element.get("class") or "") for k in _CUT_OFF_EXEMPT_CLASSES):
        return False
    left, top, right, bottom = element["bounds"]
    h, w = bottom - top, right - left
    return h < height * _CUT_OFF_MAX_HEIGHT_FRACTION and w >= 2 * h


def find_unlisted_bottom_band(
    elements: list[dict[str, Any]],
    width: int,
    height: int,
    image_bytes: bytes | None = None,
) -> tuple[int, int] | None:
    """The bottom strip of the screen below every listed element, when it holds pixels.

    A bar pinned to the bottom (the primary action button of a form, cart or
    checkout) is often drawn without its own accessibility node, so the list
    ends above it. Returns ``(top, bottom)`` in screen pixels when the gap from
    the lowest listed element to the navigation area is tall enough to hold such
    a bar and is not a blank background; ``None`` otherwise. App-agnostic: only
    geometry and pixel variance are used.
    """
    boxes = [el["bounds"] for el in elements if el.get("bounds")]
    if not boxes or not width or not height:
        return None
    top = max(b for _l, _t, _r, b in boxes)
    bottom = int(height * (1 - _BOTTOM_BAND_NAV_FRACTION))
    if bottom - top < height * _BOTTOM_BAND_MIN_FRACTION:
        return None
    if image_bytes:
        try:
            from PIL import ImageStat

            img = Image.open(io.BytesIO(image_bytes)).convert("L")
            sy = img.height / height
            band = img.crop((0, int(top * sy), img.width, int(bottom * sy)))
            band.thumbnail((256, 256))
            if ImageStat.Stat(band).stddev[0] < _BOTTOM_BAND_MIN_STDDEV:
                return None  # flat background: nothing drawn there
        except Exception:
            pass
    return top, bottom


def _merge_ocr_words(ocr_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Joins OCR boxes of one text line that the detector split into words.

    Two boxes merge when they overlap vertically by more than half the shorter
    box and the horizontal gap is under one line height ("Place" + "Order").
    """
    boxes = []
    for res in ocr_results or []:
        box = _ocr_box(res)
        if box:
            boxes.append([*box, str(res.get("text") or "").strip(), float(res.get("score", 1.0) or 0), bool(res.get("unread"))])
    boxes.sort(key=lambda x: (x[1], x[0]))
    merged: list[list[Any]] = []
    for b in boxes:
        for m in merged:
            line_h = min(b[3] - b[1], m[3] - m[1])
            v_overlap = min(b[3], m[3]) - max(b[1], m[1])
            gap = b[0] - m[2]
            if line_h > 0 and v_overlap > line_h / 2 and -line_h / 2 <= gap < line_h:
                m[0], m[1] = min(m[0], b[0]), min(m[1], b[1])
                m[2], m[3] = max(m[2], b[2]), max(m[3], b[3])
                m[4] = f"{m[4]} {b[4]}".strip()
                m[5] = min(m[5], b[5])
                m[6] = m[6] or b[6]
                break
        else:
            merged.append(b)
    return [
        {"text": t, "score": sc, "position": [{"x": l, "y": tp}, {"x": r, "y": tp}, {"x": r, "y": bt}, {"x": l, "y": bt}],
         **({"unread": True} if unread else {})}
        for l, tp, r, bt, t, sc, unread in merged
    ]


def gap_fill_ocr(
    ocr_results: list[dict[str, Any]],
    elements: list[dict[str, Any]],
    width: int,
    height: int,
    normalize_bounds: Any = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Compact ``[n] ocr "text" l,t,r,b`` lines for OCR text outside every listed element.

    Fills only the gaps of the accessibility tree (a floating button the tree does
    not declare); text already covered by a listed element is left out, so the
    list stays as the tree wrote it. ``ocr_results`` use the local OCR shape
    (``text``, ``score``, ``position`` corner points in screen pixels).
    """
    if normalize_bounds is None:
        def normalize_bounds(left, top, right, bottom):
            return (f"{int(max(0, min(1000, left * 1000 / width)))},{int(max(0, min(1000, top * 1000 / height)))},"
                    f"{int(max(0, min(1000, right * 1000 / width)))},{int(max(0, min(1000, bottom * 1000 / height)))}")
    taken = [el["bounds"] for el in elements if el.get("bounds")]
    m = _GAP_OCR_MARGIN_PX
    idx = max((int(el.get("index") or 0) for el in elements), default=0) + 1
    lines: list[str] = []
    found: list[dict[str, Any]] = []
    for res in _merge_ocr_words(ocr_results):
        if len(found) >= _MAX_GAP_OCR_LINES:
            break
        text = str(res.get("text") or "").strip()
        if len(text) < 2 or float(res.get("score") or 0) < _GAP_OCR_MIN_SCORE:
            continue
        pts = res.get("position") or []
        if len(pts) < 2:
            continue
        xs, ys = [int(p["x"]) for p in pts], [int(p["y"]) for p in pts]
        left, top, right, bottom = max(0, min(xs)), max(0, min(ys)), min(width, max(xs)), min(height, max(ys))
        if right <= left or bottom <= top or bottom <= height * _STATUS_BAR_FRACTION:
            continue
        if any(left < r + m and right > l - m and top < b + m and bottom > t - m for l, t, r, b in taken):
            continue
        cx, cy = get_center_coordinates(left, top, right, bottom)
        quoted = '"' + text.replace('"', "'") + '"'
        lines.append(f"[{idx}] ocr {quoted} {normalize_bounds(left, top, right, bottom)}")
        found.append({
            "index": idx, "center": [cx, cy], "text": text, "bounds": [left, top, right, bottom],
            "class": None, "resource_id": None, "is_ocr": True, "ocr_virtual": True,
        })
        taken.append((left, top, right, bottom))
        idx += 1
    return lines, found


#: A labelled element larger than this share of the screen (a card or panel
#: whose label is only a summary) does not swallow the OCR text inside it;
#: that text is then checked by wording instead.
_OCR_MERGE_MAX_OWNER_AREA = 0.25
#: OCR text repeating a labelled element's wording within this share of the
#: screen height of it is the same text read twice (bounds of node and pixels differ).
_OCR_MERGE_NEAR_FRACTION = 0.03
_MAX_OCR_LABEL_CHARS = 60
_MAX_OCR_MERGE_LINES = 25


def _same_wording(a: str, b: str) -> bool:
    ka, kb = _text_key(a), _text_key(b)
    if len(ka) < 2 or len(kb) < 2:
        return False
    if ka in kb or kb in ka:
        return True
    return difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.8


def merge_ocr_into_list(
    list_text: str,
    elements: list[dict[str, Any]],
    ocr_results: list[dict[str, Any]],
    width: int,
    height: int,
    refine: Any = None,
    ignore_boxes: list[tuple[int, int, int, int]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """XML-first fusion of local OCR into the compact element list (in place on ``elements``).

    For each OCR line (words of one line joined first):
    1. It overlaps a labelled element (not a large panel) -> the XML text wins, dropped.
    2. Its wording repeats a labelled element close by -> same text, dropped.
    3. Its center lies in an unlabelled control (``(button)``, ``(icon)``) -> it
       becomes that control's label: ``[n] (button) ocr "Place Order" ...``.
    4. Otherwise it is text no element covers -> ``[n] ocr "..."`` element.
    Low-confidence lines, one-character lines and the status bar are ignored.
    ``refine(lines) -> texts`` (optional) reads the lines that survive the
    position check of rule 1, before the wording checks: located-but-unread
    lines (``"unread"``, VietOCR mode) are read only here, so text the tree
    already covers is never recognised. ``lines`` are ``{"text", "box"}`` dicts
    in screen pixels. Unread lines without ``refine`` are dropped.
    Returns the new list text and counts for the debug log.
    """
    stats: dict[str, Any] = {
        "duplicate": 0, "labeled": 0, "added": 0, "read": 0, "labeled_texts": [], "added_texts": [],
    }
    if not ocr_results or not width or not height:
        return list_text, stats
    screen_area = float(width * height)
    m = _GAP_OCR_MARGIN_PX
    near = height * _OCR_MERGE_NEAR_FRACTION
    labeled = [el for el in elements if el.get("bounds") and str(el.get("text") or "").strip() and not el.get("is_icon")]
    icons = [el for el in elements if el.get("bounds") and el.get("is_icon")]
    icon_texts: dict[int, list[str]] = {}
    new_lines: list[str] = []
    idx = max((int(el.get("index") or 0) for el in elements), default=0) + 1

    def overlaps(a, b, margin=0.0) -> bool:
        return a[0] < b[2] + margin and a[2] > b[0] - margin and a[1] < b[3] + margin and a[3] > b[1] - margin

    small_owners = [
        el["bounds"] for el in labeled
        if (el["bounds"][2] - el["bounds"][0]) * (el["bounds"][3] - el["bounds"][1]) <= screen_area * _OCR_MERGE_MAX_OWNER_AREA
    ]
    rows: list[list[Any]] = []  # [text, box, covered_by_xml, unread]
    for res in _merge_ocr_words(ocr_results):
        text = str(res.get("text") or "").strip()
        box = _ocr_box(res)
        if not box or float(res.get("score") or 0) < _GAP_OCR_MIN_SCORE:
            continue
        left, top, right, bottom = max(0, box[0]), max(0, box[1]), min(width, box[2]), min(height, box[3])
        if right <= left or bottom <= top or bottom <= height * _STATUS_BAR_FRACTION:
            continue
        box = (left, top, right, bottom)
        rows.append([text, box, any(overlaps(box, b, m) for b in small_owners), bool(res.get("unread"))])

    if refine is not None:
        todo = [r for r in rows if not r[2] and (r[3] or not r[0])]
        if todo:
            try:
                texts = refine([{"text": r[0], "box": r[1]} for r in todo])
                for r, new in zip(todo, texts or []):
                    r[0] = str(new or "").strip()
                    stats["read"] += 1
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.debug(f"OCR read step failed, unread lines dropped: {e}")

    for text, box, covered, unread in rows:
        left, top, right, bottom = box
        if sum(ch.isalnum() for ch in text) < 2 and not (covered and unread):
            continue  # rating stars, separators, a lone glyph, a line left unread
        # 1-2. Already in the tree: the accessibility text is exact, OCR is not.
        duplicate = covered or any(
            overlaps(box, el["bounds"], near) and _same_wording(text, str(el.get("text"))) for el in labeled
        )
        if duplicate:
            stats["duplicate"] += 1
            continue
        # 3. Inside an unlabelled control: OCR names it (smallest control wins).
        cx, cy = (left + right) / 2, (top + bottom) / 2
        if ignore_boxes and any(bx[0] <= cx <= bx[2] and bx[1] <= cy <= bx[3] for bx in ignore_boxes):
            stats["duplicate"] += 1
            continue
        owners = [el for el in icons if el["bounds"][0] <= cx <= el["bounds"][2] and el["bounds"][1] <= cy <= el["bounds"][3]]
        if owners:
            owner = min(owners, key=lambda e: (e["bounds"][2] - e["bounds"][0]) * (e["bounds"][3] - e["bounds"][1]))
            icon_texts.setdefault(int(owner["index"]), []).append(text)
            continue
        # 4. Text no element covers (a pinned button without a node, canvas text).
        if any(overlaps(box, el["bounds"], m) for el in icons):
            stats["duplicate"] += 1  # partly on a control whose label it is not: skip, never guess
            continue
        if stats["added"] >= _MAX_OCR_MERGE_LINES:
            continue
        ccx, ccy = get_center_coordinates(left, top, right, bottom)
        quoted = '"' + text.replace('"', "'") + '"'
        new_lines.append(
            f"[{idx}] ocr {quoted} "
            f"{int(left * 1000 / width)},{int(top * 1000 / height)},{int(right * 1000 / width)},{int(bottom * 1000 / height)}"
        )
        elements.append({
            "index": idx, "center": [ccx, ccy], "text": text, "bounds": [left, top, right, bottom],
            "class": None, "resource_id": None, "is_ocr": True, "ocr_virtual": True,
        })
        labeled.append(elements[-1])  # a repeat of the same text further on is a duplicate
        stats["added"] += 1
        stats["added_texts"].append(text[:40])
        idx += 1

    if icon_texts:
        by_index = {int(el["index"]): el for el in icons}
        out = []
        for line in list_text.splitlines():
            mt = re.match(r"\[(\d+)\] \(([^)]+)\) ", line)
            if mt and int(mt.group(1)) in icon_texts:
                label = " ".join(icon_texts[int(mt.group(1))])[:_MAX_OCR_LABEL_CHARS]
                quoted = '"' + label.replace('"', "'") + '"'
                line = f"{line[:mt.end()]}ocr {quoted} {line[mt.end():]}"
                el = by_index[int(mt.group(1))]
                el["text"] = label
                el["ocr_label"] = True
                stats["labeled"] += 1
                stats["labeled_texts"].append(label[:40])
            out.append(line)
        list_text = "\n".join(out)
    if new_lines:
        list_text = (list_text + "\n" if list_text else "") + "\n".join(new_lines)
    # Attach nearby OCR/XML labels to unlabeled input fields that still lack a label (L-050).
    unlabeled_inputs = [
        el for el in elements if _is_input_element(el) and not str(el.get("text") or "").strip()
    ]
    if unlabeled_inputs:
        labeled_now = [
            el for el in elements if str(el.get("text") or "").strip() and el.get("bounds") and el.get("center")
        ]
        updated_labels: dict[int, str] = {}
        for inp in unlabeled_inputs:
            if inp.get("nearby_label"):
                continue
            lbl = _nearest_input_label(inp, labeled_now, width, height)
            if lbl:
                inp["nearby_label"] = lbl
                updated_labels[int(inp["index"])] = lbl
        if updated_labels:
            patched = []
            for line in list_text.splitlines():
                mt = re.match(r"\[(\d+)\] \(input field\) ", line)
                if mt and int(mt.group(1)) in updated_labels:
                    lbl_q = '"' + updated_labels[int(mt.group(1))].replace('"', "'") + '"'
                    line = f"[{mt.group(1)}] (input field: {lbl_q}) {line[mt.end():]}"
                patched.append(line)
            list_text = "\n".join(patched)
    return list_text, stats


def _is_input_element(el: dict[str, Any]) -> bool:
    cls = str(el.get("class") or "")
    return "EditText" in cls or _is_truthy(el.get("editable"))


def _nearest_input_label(
    element: dict[str, Any],
    labeled: list[dict[str, Any]],
    width: int,
    height: int,
) -> str:
    """Finds a nearby XML/OCR text label inside, just above (~1 line), or just left of an unlabeled input field (L-050)."""
    bounds = element.get("bounds")
    if not bounds or not labeled:
        return ""
    l, t, r, b = bounds
    cx, cy = (l + r) / 2.0, (t + b) / 2.0
    h = max(1, b - t)

    def _clean(item: dict[str, Any]) -> str:
        return str(item.get("text") or "").strip().replace("\n", " ")[:40]

    # 1. Label sitting inside the field frame (e.g. Material OutlinedTextField label)
    inside = [
        it for it in labeled
        if it is not element and l - 4 <= it["center"][0] <= r + 4 and t - 4 <= it["center"][1] <= b + 4
        and _clean(it)
    ]
    if inside:
        best = min(inside, key=lambda it: (it["bounds"][1], abs(it["center"][0] - cx)))
        return _clean(best)

    # 2. Label immediately above within ~1 row and horizontally overlapping
    above_gap = max(45, int(height * 0.05), h)
    above = [
        it for it in labeled
        if it is not element
        and 0 <= t - it["bounds"][3] <= above_gap
        and min(r, it["bounds"][2]) - max(l, it["bounds"][0]) > 0
        and _clean(it)
    ]
    if above:
        best = min(above, key=lambda it: (t - it["bounds"][3], abs(it["center"][0] - cx)))
        return _clean(best)

    # 3. Label immediately to the left on the same row
    left_gap = max(40, int(width * 0.2))
    same_row = [
        it for it in labeled
        if it is not element
        and abs(cy - it["center"][1]) <= max(35, h * 0.6)
        and 0 <= l - it["bounds"][2] <= left_gap
        and _clean(it)
    ]
    if same_row:
        best = min(same_row, key=lambda it: l - it["bounds"][2])
        return _clean(best)
    return ""


def _is_status_bar_node(node: dict[str, Any], height: int, bounds: tuple[int, int, int, int] | None) -> bool:
    """A system UI node drawn inside the top status-bar band."""
    if not bounds or str(node.get("package") or "") != "com.android.systemui":
        return False
    return bounds[3] <= height * _STATUS_BAR_FRACTION


_IME_ACTION_LABELS = frozenset({
    "done", "next", "search", "go", "send", "enter", "return", "previous", "prev",
    "submit", "ok", "xong", "tiep", "tim", "tim kiem", "gui", "di",
    "hide keyboard", "close keyboard", "dismiss keyboard", "collapse keyboard",
    "an ban phim", "dong ban phim",
})
_IME_ACTION_RID_TOKENS = ("key_pos_ime_action", "ime_action", "hide_keyboard", "close_keyboard")


def _keyboard_window_info(
    fused_xml: list[dict[str, Any]], width: int, height: int
) -> tuple[set[int], tuple[int, int, int, int] | None]:
    """Detects soft-keyboard (IME) nodes in ``fused_xml`` across any keyboard app (L-053)."""
    if not fused_xml or width <= 0 or height <= 0:
        return set(), None
    y_floor = int(height * 0.38)
    pkg_stats: dict[str, dict[str, Any]] = {}
    node_entries: list[tuple[dict[str, Any], str, tuple[int, int, int, int]]] = []
    for n in fused_xml:
        if not isinstance(n, dict) or n.get("ocr_virtual"):
            continue
        b = parse_bounds(n.get("bounds"))
        if not b or b[2] <= b[0] or b[3] <= b[1]:
            continue
        pkg = str(n.get("package") or "").strip()
        if not pkg or pkg == "com.android.systemui":
            continue
        node_entries.append((n, pkg, b))
        st = pkg_stats.setdefault(
            pkg, {"has_editable": False, "top_active": 0, "lower_boxes": [], "lower_leaf_keys": 0}
        )
        if _is_input_element(n):
            st["has_editable"] = True
        is_active = bool(
            _is_truthy(n.get("clickable"))
            or _is_truthy(n.get("long-clickable"))
            or _is_input_element(n)
            or (n.get("text") or n.get("content-desc") or n.get("hint") or "").strip()
        )
        if b[1] < y_floor and is_active:
            st["top_active"] += 1
        if b[1] >= y_floor:
            st["lower_boxes"].append((max(0, b[0]), max(0, b[1]), min(width, b[2]), min(height, b[3])))
            if is_active:
                st["lower_leaf_keys"] += 1

    if len(pkg_stats) < 2:
        return set(), None

    kb_pkgs: set[str] = set()
    for pkg, st in pkg_stats.items():
        if st["has_editable"] or st["top_active"] > 0 or not st["lower_boxes"]:
            continue
        ux1 = min(bx[0] for bx in st["lower_boxes"])
        ux2 = max(bx[2] for bx in st["lower_boxes"])
        if ux2 - ux1 < width * 0.75:
            continue
        pkg_l = pkg.lower()
        looks_like_ime_pkg = any(tok in pkg_l for tok in ("inputmethod", "keyboard", "latinime", ".ime"))
        if st["lower_leaf_keys"] >= 6 or (looks_like_ime_pkg and st["lower_leaf_keys"] >= 2):
            kb_pkgs.add(pkg)

    if not kb_pkgs or len(kb_pkgs) >= len(pkg_stats):
        return set(), None
    if not any(pkg not in kb_pkgs and (st["top_active"] > 0 or st["has_editable"]) for pkg, st in pkg_stats.items()):
        return set(), None

    kb_ids = {id(n) for n, pkg, _ in node_entries if pkg in kb_pkgs}
    lower_all = [
        (max(0, b[0]), max(0, b[1]), min(width, b[2]), min(height, b[3]))
        for n, pkg, b in node_entries
        if pkg in kb_pkgs and b[1] >= y_floor
    ]
    if not lower_all:
        return set(), None
    kb_bounds = (
        min(bx[0] for bx in lower_all),
        min(bx[1] for bx in lower_all),
        max(bx[2] for bx in lower_all),
        max(bx[3] for bx in lower_all),
    )
    return kb_ids, kb_bounds


def _is_ime_action_key(node: dict[str, Any]) -> bool:
    """True for named IME action/dismiss keys kept individually when folding keyboard keys (L-053)."""
    from artemis.workflow.targets import fold

    rid = str(node.get("resource-id") or "").lower()
    is_interactive = bool(
        _is_truthy(node.get("clickable")) or _is_truthy(node.get("long-clickable"))
    )
    if any(tok in rid for tok in _IME_ACTION_RID_TOKENS):
        return True
    raw = str(node.get("text") or node.get("content-desc") or "").strip()
    if not raw or not is_interactive:
        return False
    norm = re.sub(r"\s+", " ", fold(raw))
    if norm in _IME_ACTION_LABELS:
        return True
    return ("hide" in norm and "keyboard" in norm) or ("an" in norm and "ban phim" in norm)


def _icon_kind(node: dict[str, Any]) -> str:
    """Short role word for an unlabeled control, from its Android class."""
    cls = str(node.get("class") or "")
    if "EditText" in cls or _is_truthy(node.get("editable")):
        return "input field"
    if any(k in cls for k in ("Switch", "CheckBox", "RadioButton", "ToggleButton")):
        return "toggle"
    if "Image" in cls:
        return "icon"
    return "button"


def format_minimal_list_with_elements(
    fused_xml: list[dict[str, Any]],
    width: int = 1080,
    height: int = 2400,
    compact: bool = False,
    hidden: set[int] | None = None,
    mark_required: bool = True,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Formats fused XML hierarchy into a minimal list with sequence numbers,

    and returns structured element metadata & labels for drawing/validation.

    ``compact`` (the Flash runner's token-lean form) keeps the same elements and
    indices semantics but: drops system status-bar nodes, writes
    ``[n] "text" l,t,r,b`` instead of ``[n] Text: 'text' | Bounds: [l,t][r,b]``,
    prints ``checked``/``enabled`` only when they carry information (a checkable
    control, a disabled one), shortens the overlap warning, and appends unlabeled
    clickable controls (``[n] (icon) l,t,r,b``) so they can be addressed by index.

    ``hidden`` (compact only): positions in ``fused_xml`` (after the status-bar
    filter is NOT applied: positions refer to the list as passed in) of nodes a
    layer on top verifiably covers (see :func:`verified_overlay_hidden`); they are
    left out and summarized in one line.
    """
    lines = []
    elements = []
    labels = []
    idx = 1
    added_items = []  # list of tuples: (text, cx, cy)
    items_bounds = []  # list of tuples: (idx, (left, top, right, bottom))

    def state_token(value: Any) -> str:
        token = str(value or "").strip().lower()
        return token if token in ("true", "false") else "?"

    def normalize_bounds(left: int, top: int, right: int, bottom: int) -> str:
        n_left = int(max(0, min(1000, left * 1000 / width)))
        n_top = int(max(0, min(1000, top * 1000 / height)))
        n_right = int(max(0, min(1000, right * 1000 / width)))
        n_bottom = int(max(0, min(1000, bottom * 1000 / height)))
        if compact:
            return f"{n_left},{n_top},{n_right},{n_bottom}"
        return f"[{n_left},{n_top}][{n_right},{n_bottom}]"

    def quote(value: str) -> str:
        return '"' + value.replace('"', "'").replace("\n", " ") + '"'

    # A bottom sheet / dialog drawn over the screen: what it verifiably covers
    # cannot be tapped, so the compact list leaves it out and says so in one line.
    overlay_hidden = 0
    if compact and hidden:
        hidden_pos = {i for i in hidden if 0 <= i < len(fused_xml)}
        overlay_hidden = len(hidden_pos)
        fused_xml = [n for i, n in enumerate(fused_xml) if i not in hidden_pos]
    if compact:
        fused_xml = [
            n for n in fused_xml
            if not _is_status_bar_node(n, height, parse_bounds(n.get("bounds")))
        ]
    kb_ids, kb_bounds = (
        _keyboard_window_info(fused_xml, width, height) if compact else (set(), None)
    )
    if kb_ids:
        app_xml = [n for n in fused_xml if id(n) not in kb_ids]
        kb_action_xml = [n for n in fused_xml if id(n) in kb_ids and _is_ime_action_key(n)]
        kept_kb_ids = {id(n) for n in kb_action_xml}
        folded_kb_count = sum(
            1
            for n in fused_xml
            if id(n) in kb_ids
            and id(n) not in kept_kb_ids
            and (
                _is_truthy(n.get("clickable"))
                or _is_truthy(n.get("long-clickable"))
                or bool((n.get("text") or n.get("content-desc") or "").strip())
            )
        )
    else:
        app_xml = fused_xml
        kb_action_xml = []
        folded_kb_count = 0

    required_ids, star_ids = (
        _required_markers(app_xml, width) if compact and mark_required else (set(), set())
    )
    # Row/container selection state (L-040): a RecyclerView/LinearLayout row often
    # carries selected=true or checked=true while its child TextView does not.
    screen_area = max(1, width * height)
    selected_row_boxes: list[tuple[int, int, int, int]] = []
    checked_row_boxes: list[tuple[int, int, int, int]] = []
    for container in app_xml:
        if str(container.get("text") or container.get("content-desc") or "").strip():
            continue
        if _is_truthy(container.get("scrollable")):
            continue
        cb = parse_bounds(container.get("bounds"))
        if not cb or cb[2] <= cb[0] or cb[3] <= cb[1]:
            continue
        if (cb[2] - cb[0]) * (cb[3] - cb[1]) > int(screen_area * 0.25) or (cb[3] - cb[1]) > int(height * 0.35):
            continue
        if _is_truthy(container.get("selected")):
            selected_row_boxes.append(cb)
        if _is_truthy(container.get("checked")):
            checked_row_boxes.append(cb)

    def _enclosed_by(b: tuple[int, int, int, int], boxes: list[tuple[int, int, int, int]]) -> bool:
        return any(
            box[0] - 4 <= b[0] and box[1] - 4 <= b[1] and b[2] <= box[2] + 4 and b[3] <= box[3] + 4
            for box in boxes
        )

    def is_duplicate(text_val: str, cx: int, cy: int) -> bool:
        text_clean = text_val.strip()
        for ex_text, ex_cx, ex_cy in added_items:
            if ex_text == text_clean:
                dist = ((cx - ex_cx) ** 2 + (cy - ex_cy) ** 2) ** 0.5
                if dist < 8.0:
                    return True
        return False

    for node in app_xml:
        if id(node) in star_ids:
            continue  # folded into its label as "(required)"
        text = node.get("text") or node.get("content-desc") or ""
        bounds_str = node.get("bounds")
        bounds = parse_bounds(bounds_str)

        ocr_elements = node.get("ocr_elements")
        registered = False

        if ocr_elements:
            for ocr in ocr_elements:
                ocr_bounds = parse_bounds(ocr.get("bounds"))
                if ocr_bounds:
                    left, top, right, bottom = ocr_bounds
                    if right <= left or bottom <= top or right <= 0 or bottom <= 0 or left >= width or top >= height:
                        continue
                    cx, cy = get_center_coordinates(left, top, right, bottom)
                    if is_duplicate(ocr["text"], cx, cy):
                        continue

                    norm_bounds = normalize_bounds(left, top, right, bottom)
                    if compact:
                        lines.append(f"[{idx}] ocr {quote(ocr['text'])} {norm_bounds}")
                    else:
                        lines.append(f"[{idx}] OCR Text: '{ocr['text']}' | Bounds: {norm_bounds}")
                    items_bounds.append((idx, (left, top, right, bottom)))

                    elements.append(
                        {
                            "index": idx,
                            "center": [cx, cy],
                            "text": ocr["text"].strip(),
                            "bounds": [left, top, right, bottom],
                            "class": node.get("class"),
                            "resource_id": node.get("resource-id"),
                            "is_ocr": True,
                            **({"ocr_virtual": True} if node.get("ocr_virtual") else {}),
                        }
                    )
                    labels.append(str(idx))
                    added_items.append((ocr["text"].strip(), cx, cy))
                    idx += 1
                    registered = True

        # An empty input field carries no text but usually a hint ("Search settings");
        # a rejected one carries an error ("Password too short"). Both come from the
        # accessibility helper's dump and are exactly what the agent needs to see.
        hint = str(node.get("hint") or "").strip()
        error = str(node.get("error") or "").strip()
        shown = text.strip() or hint
        if not registered and shown and bounds:
            left, top, right, bottom = bounds
            if right <= left or bottom <= top or right <= 0 or bottom <= 0 or left >= width or top >= height:
                continue
            if idx > 80:
                is_interactive = bool(
                    _is_truthy(node.get("clickable"))
                    or _is_truthy(node.get("focusable"))
                    or _is_truthy(node.get("checked"))
                    or _is_truthy(node.get("selected"))
                    or error
                    or "Button" in str(node.get("class") or "")
                    or "EditText" in str(node.get("class") or "")
                )
                if not is_interactive and idx > 100:
                    continue
            cx, cy = get_center_coordinates(left, top, right, bottom)
            if is_duplicate(shown, cx, cy):
                continue

            norm_bounds = normalize_bounds(left, top, right, bottom)
            kind = "Text" if text.strip() else "Hint"
            checkable = str(node.get("checkable") or "").strip().lower() == "true"
            node_checked_true = _is_truthy(node.get("checked")) or _enclosed_by(bounds, checked_row_boxes)
            node_selected = _is_truthy(node.get("selected")) or _enclosed_by(bounds, selected_row_boxes)
            if compact:
                prefix = "" if text.strip() else "hint "
                line = f"[{idx}] {prefix}{quote(shown)} {norm_bounds}"
                # Only informative state: ON/OFF of a checkable control (#187),
                # selected rows/tabs (L-040), and a disabled control.
                if checkable or node_checked_true:
                    cval = "true" if node_checked_true else state_token(node.get("checked"))
                    line += f" checked:{cval}"
                if node_selected:
                    line += " (selected)"
                if str(node.get("enabled") or "").strip().lower() == "false":
                    line += " enabled:false"
                if error:
                    line += f" error:{quote(error)}"
                if id(node) in required_ids:
                    line += " (required)"
            else:
                line = f"[{idx}] {kind}: '{shown}' | Bounds: {norm_bounds}"
            # Explicit XML state for stateful controls: an ON and an OFF
            # labeled Switch are otherwise identical text (#187). Absent or
            # invalid attributes render as '?' — unknown, not implicitly OFF.
            if compact:
                pass
            elif checkable or node.get("checked") is not None or node_checked_true:
                line += " | " + " ".join(
                    f"{a}:{'true' if a == 'checked' and node_checked_true else state_token(node.get(a))}"
                    for a in ("checked", "enabled")
                )
            elif str(node.get("enabled") or "").strip().lower() == "false":
                line += " | enabled:false"
            if not compact and node_selected:
                line += " (selected)"
            if error and not compact:
                line += f" | Error: '{error}'"
            lines.append(line)
            items_bounds.append((idx, (left, top, right, bottom)))

            element = {
                "index": idx,
                "center": [cx, cy],
                "text": shown,
                "bounds": [left, top, right, bottom],
                "class": node.get("class"),
                "resource_id": node.get("resource-id"),
                "is_ocr": False,
            }
            if hint:
                element["hint"] = hint
            # Raw uiautomator state attributes, carried on the element for
            # code-side verification (signatures, state predicates).
            for state_attr in ("checked", "checkable", "enabled", "selected", "package"):
                if node.get(state_attr) is not None:
                    element[state_attr] = node.get(state_attr)
            if node_selected and not _is_truthy(element.get("selected")):
                element["selected"] = True
            if node_checked_true and not _is_truthy(element.get("checked")):
                element["checked"] = True
            if not text.strip():
                element["is_hint"] = True
            if error:
                element["error"] = error
            elements.append(element)
            labels.append(str(idx))
            added_items.append((shown, cx, cy))
            idx += 1

    lines = _inject_mutual_occlusion_warnings(lines, items_bounds, elements=elements)
    if compact:
        lines = [
            re.sub(r" \(WARNING: may overlap with (.+), possible occlusion\)$", r" (overlaps \1, may be occluded)", ln)
            for ln in lines
        ]
        # Unlabeled controls last, after the occlusion pass: an icon drawn over
        # a card is expected layout, and the guard must not start blocking
        # labeled targets because of them.
        icon_lines, icon_elements = _unlabeled_controls(
            app_xml, width, height, elements, start_idx=idx, normalize_bounds=normalize_bounds,
            quote=quote,
        )
        lines.extend(icon_lines)
        elements.extend(icon_elements)
        labels.extend(str(el["index"]) for el in icon_elements)
        idx += len(icon_elements)
        if kb_bounds is not None:
            kb_norm = normalize_bounds(*kb_bounds)
            kb_summary = (
                f"(keyboard open {kb_norm}: {folded_kb_count} keys folded"
                + ("; action keys listed below" if kb_action_xml else "")
                + "; use press_key('BACK') to hide)"
            )
            lines.append(kb_summary)
            for k_node in kb_action_xml:
                kb_b = parse_bounds(k_node.get("bounds"))
                if not kb_b:
                    continue
                k_left, k_top, k_right, k_bottom = (
                    max(0, kb_b[0]), max(0, kb_b[1]), min(width, kb_b[2]), min(height, kb_b[3])
                )
                if k_right <= k_left or k_bottom <= k_top:
                    continue
                k_shown = str(
                    k_node.get("text") or k_node.get("content-desc") or k_node.get("hint") or "Action"
                ).strip()
                k_cx, k_cy = get_center_coordinates(k_left, k_top, k_right, k_bottom)
                lines.append(f"[{idx}] {quote(k_shown)} {normalize_bounds(k_left, k_top, k_right, k_bottom)}")
                k_el = {
                    "index": idx,
                    "center": [k_cx, k_cy],
                    "text": k_shown,
                    "bounds": [k_left, k_top, k_right, k_bottom],
                    "class": k_node.get("class"),
                    "resource_id": k_node.get("resource-id"),
                    "is_ocr": False,
                    "is_keyboard": True,
                }
                for state_attr in ("checked", "checkable", "enabled", "selected", "package"):
                    if k_node.get(state_attr) is not None:
                        k_el[state_attr] = k_node.get(state_attr)
                elements.append(k_el)
                labels.append(str(idx))
                idx += 1
        if overlay_hidden:
            lines.append(
                f"(a layer on top covers the screen below: {overlay_hidden} elements under it are not listed)"
            )
    return "\n".join(lines), elements, labels


def _unlabeled_controls(
    fused_xml: list[dict[str, Any]],
    width: int,
    height: int,
    listed: list[dict[str, Any]],
    *,
    start_idx: int,
    normalize_bounds: Any,
    quote: Any = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Clickable controls with no text, hint or description (back arrows, "+",
    search fields, toggles), as compact lines plus element records.

    Skipped: containers (too large, or holding an already listed interactive
    control's center), slivers, and a wrapper that repeats the center of a control
    already taken. Editable inputs keep their slot even when a static text label
    sits inside their bounds (L-050).
    """
    lines: list[str] = []
    found: list[dict[str, Any]] = []
    screen_area = max(1, width * height)
    interactive_listed_centers = [
        el["center"]
        for el in listed
        if el.get("center")
        and (
            _is_input_element(el)
            or any(k in str(el.get("class") or "") for k in ("Button", "ImageButton", "Switch", "CheckBox"))
        )
    ]
    all_listed_centers = [el["center"] for el in listed if el.get("center")]
    labeled_listed = [el for el in listed if str(el.get("text") or "").strip() and el.get("bounds") and el.get("center")]
    candidates: list[tuple[int, dict[str, Any]]] = []
    for pos, node in enumerate(fused_xml):
        if len(candidates) >= _MAX_ICON_LINES:
            break
        if (node.get("text") or node.get("content-desc") or node.get("hint") or "").strip():
            continue
        if node.get("ocr_elements") or node.get("ocr_virtual"):
            continue
        cls = str(node.get("class") or "")
        is_input = "EditText" in cls or _is_truthy(node.get("editable"))
        if not (_is_truthy(node.get("clickable")) or _is_truthy(node.get("long-clickable")) or is_input):
            continue
        bounds = parse_bounds(node.get("bounds"))
        if not bounds:
            continue
        left, top, right, bottom = bounds
        left, top = max(0, left), max(0, top)
        right, bottom = min(width, right), min(height, bottom)
        w, h = right - left, bottom - top
        if w < _ICON_MIN_SIDE_PX or h < _ICON_MIN_SIDE_PX:
            continue
        cap = _INPUT_MAX_AREA_FRACTION if is_input else _ICON_MAX_AREA_FRACTION
        if w * h > screen_area * cap:
            continue
        if _is_status_bar_node(node, height, (left, top, right, bottom)):
            continue
        check_centers = interactive_listed_centers if is_input else all_listed_centers
        if any(left <= cx <= right and top <= cy <= bottom for cx, cy in check_centers):
            continue
        cx, cy = get_center_coordinates(left, top, right, bottom)
        if any(abs(cx - c["center"][0]) < 8 and abs(cy - c["center"][1]) < 8 for _, c in candidates):
            continue
        element = {
            "center": [cx, cy],
            "text": "",
            "bounds": [left, top, right, bottom],
            "class": node.get("class"),
            "resource_id": node.get("resource-id"),
            "is_ocr": False,
            "is_icon": True,
        }
        for state_attr in ("checked", "checkable", "enabled", "selected", "package", "editable", "password"):
            if node.get(state_attr) is not None:
                element[state_attr] = node.get(state_attr)
        candidates.append((pos, element))

    # A clickable wrapper around another unlabeled control: keep only the
    # inner (leaf) control, the wrapper adds a second target for one spot.
    def _contains(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
        ol, ot, orr, ob = outer["bounds"]
        il, it, ir, ib = inner["bounds"]
        return outer is not inner and ol <= il and ot <= it and orr >= ir and ob >= ib and (
            (orr - ol) * (ob - ot) > (ir - il) * (ib - it))

    kept = [(pos, el) for pos, el in candidates
            if not any(_contains(el, other) for _, other in candidates)]

    idx = start_idx
    for pos, element in kept:
        left, top, right, bottom = element["bounds"]
        kind = _icon_kind(element)
        nearby = _nearest_input_label(element, labeled_listed, width, height) if kind == "input field" else ""
        if nearby:
            element["nearby_label"] = nearby
            q_lbl = quote(nearby) if callable(quote) else '"' + nearby.replace('"', "'") + '"'
            line = f"[{idx}] ({kind}: {q_lbl}) {normalize_bounds(left, top, right, bottom)}"
        else:
            line = f"[{idx}] ({kind}) {normalize_bounds(left, top, right, bottom)}"
        if kind == "toggle":
            line += f" checked:{str(element.get('checked') or '?').strip().lower()}"
        if str(element.get("enabled") or "").strip().lower() == "false":
            line += " enabled:false"
        if _covered_by_later_layer(fused_xml, pos, element["bounds"]):
            element["occluded"] = True
            line += " (may be occluded)"
        if _is_cut_off_strip(element, height):
            element["cut_off"] = True
            line += " (cut off: only a thin strip is visible; scroll to see it whole)"
        element["index"] = idx
        lines.append(line)
        found.append(element)
        idx += 1
    return lines, found


def _covered_by_later_layer(
    fused_xml: list[dict[str, Any]], pos: int, icon_bounds: list[int]
) -> bool:
    """True when a node drawn after the icon (later in the flattened tree, so
    not one of its ancestors) covers at least half of it: a dialog, bottom
    sheet, scrim or floating bar on top. Only touchable or text nodes count;
    a plain layout pass-through does not block a tap.
    """
    l, t, r, b = icon_bounds
    area = max(1, (r - l) * (b - t))
    for node in fused_xml[pos + 1:]:
        if node.get("ocr_virtual"):
            continue
        if not (_is_truthy(node.get("clickable")) or _is_truthy(node.get("long-clickable"))
                or (node.get("text") or node.get("content-desc") or "").strip()):
            continue
        nb = parse_bounds(node.get("bounds"))
        if not nb:
            continue
        nl, nt, nr, nbot = nb
        if nl >= l and nt >= t and nr <= r and nbot <= b:
            continue  # a child of the icon
        iw, ih = min(r, nr) - max(l, nl), min(b, nbot) - max(t, nt)
        if iw > 0 and ih > 0 and iw * ih >= area * 0.5:
            return True
    return False


def crop_and_annotate_target(
    image_bytes: bytes,
    coords: list[int],
    crop_size: int | None = 256,
    dot_radius: int = 8,
) -> bytes:
    """Crops a square region centered around coords, and draws a red dot with black outline at coords.

    If crop_size is None, returns the full annotated image.
    """
    img = Image.open(io.BytesIO(image_bytes))
    width, height = img.size
    x, y = coords

    # Ensure coordinates are within image boundaries
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))

    if crop_size is not None:
        # Calculate crop boundaries (centered around x, y; clamped to image boundaries)
        w_crop = min(crop_size, width)
        h_crop = min(crop_size, height)

        x_min = max(0, min(x - w_crop // 2, width - w_crop))
        y_min = max(0, min(y - h_crop // 2, height - h_crop))
        x_max = x_min + w_crop
        y_max = y_min + h_crop

        # Crop
        cropped_img = img.crop((x_min, y_min, x_max, y_max))

        # Local coordinates within the cropped frame
        local_x = x - x_min
        local_y = y - y_min
    else:
        cropped_img = img
        local_x = x
        local_y = y

    # Draw solid red circle with a black outline
    draw = ImageDraw.Draw(cropped_img)
    draw.ellipse(
        [
            local_x - dot_radius,
            local_y - dot_radius,
            local_x + dot_radius,
            local_y + dot_radius,
        ],
        fill="red",
        outline="black",
        width=2 if crop_size is None else 1,
    )

    out_buf = io.BytesIO()
    if crop_size is None:
        if cropped_img.mode != "RGB":
            cropped_img = cropped_img.convert("RGB")
        cropped_img.save(out_buf, format="JPEG", quality=85)
    else:
        cropped_img.save(out_buf, format="PNG")
    return out_buf.getvalue()


def _resolve_coordinates(coord: Any, width: int, height: int) -> tuple[int, int] | None:
    """Helper to safely parse coordinates into pixel space."""
    if not isinstance(coord, (list, tuple)) or len(coord) < 2:
        return None
    try:
        x, y = float(coord[0]), float(coord[1])
        # If coordinates are normalized (0.0 <= x <= 1.0)
        if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
            px = int(round(x * width))
            py = int(round(y * height))
        elif 0 <= x <= 1000 and 0 <= y <= 1000 and (width > 1000 or height > 1000):
            # Android normalized 1000 coordinate space
            px = int(round(x * width / 1000.0))
            py = int(round(y * height / 1000.0))
        else:
            px = int(round(x))
            py = int(round(y))
        px = max(0, min(width - 1, px))
        py = max(0, min(height - 1, py))
        return px, py
    except Exception:
        return None


def draw_action_overlay_on_image(
    image_bytes: bytes,
    action_name: str,
    action_args: dict[str, Any],
) -> bytes:
    """Draws visual action indicators (tap ripples, sequence numbers, swipe arrows, focus boxes)

    on top of the before-action screenshot.

    Args:
        image_bytes: Raw JPEG/PNG image bytes of the screenshot.
        action_name: Action name (e.g. 'click', 'click_sequence', 'swipe', 'input_text').
        action_args: Action parameters dictionary.

    Returns:
        Annotated JPEG image bytes, or original image_bytes on error.
    """
    import math

    if not image_bytes:
        return image_bytes

    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        width, height = img.size
        draw = ImageDraw.Draw(img)

        # Scale indicator sizes dynamically based on screen resolution
        base_dim = min(width, height)
        dot_radius = max(18, int(base_dim * 0.025))
        line_width = max(4, int(base_dim * 0.006))

        action = (action_name or "").lower()

        # 1. Single Click / Tap
        if action in ("click", "tap", "click_coordinate", "long_click", "press"):
            coord = (
                action_args.get("target")
                or action_args.get("coordinates")
                or action_args.get("point")
            )
            pt = _resolve_coordinates(coord, width, height)
            if pt:
                x, y = pt
                # Outer white aura
                draw.ellipse(
                    [
                        x - dot_radius * 1.5,
                        y - dot_radius * 1.5,
                        x + dot_radius * 1.5,
                        y + dot_radius * 1.5,
                    ],
                    outline="white",
                    width=line_width + 2,
                )
                # Red ripple circle
                draw.ellipse(
                    [x - dot_radius, y - dot_radius, x + dot_radius, y + dot_radius],
                    fill="red",
                    outline="white",
                    width=line_width,
                )
                # Crosshair
                cross_len = int(dot_radius * 1.8)
                draw.line([(x - cross_len, y), (x + cross_len, y)], fill="white", width=line_width)
                draw.line([(x, y - cross_len), (x, y + cross_len)], fill="white", width=line_width)

        # 2. Click Sequence
        elif action in ("click_sequence", "tap_sequence"):
            sequence = (
                action_args.get("sequence")
                or action_args.get("targets")
                or action_args.get("coordinates")
                or []
            )
            if isinstance(sequence, str):
                sequence_str = sequence.strip()
                try:
                    import json

                    sequence = json.loads(sequence_str)
                except Exception:
                    try:
                        import ast

                        sequence = ast.literal_eval(sequence_str)
                    except (ValueError, TypeError, SyntaxError):
                        # Unparseable sequence string: leave it as-is; the
                        # isinstance check below skips non-list values.
                        pass

            if isinstance(sequence, list):
                pts = []
                for coord in sequence:
                    pt = _resolve_coordinates(coord, width, height)
                    if pt:
                        pts.append(pt)

                # Draw connecting lines/arrows between consecutive sequence points
                for i in range(len(pts) - 1):
                    p1, p2 = pts[i], pts[i + 1]
                    draw.line([p1, p2], fill="white", width=line_width + 3)
                    draw.line([p1, p2], fill="red", width=line_width)

                    # Arrow head
                    dx = p2[0] - p1[0]
                    dy = p2[1] - p1[1]
                    angle = math.atan2(dy, dx)
                    arrow_len = max(16, int(base_dim * 0.025))
                    arrow_angle = math.pi / 6
                    x1 = p2[0] - arrow_len * math.cos(angle - arrow_angle)
                    y1 = p2[1] - arrow_len * math.sin(angle - arrow_angle)
                    x2 = p2[0] - arrow_len * math.cos(angle + arrow_angle)
                    y2 = p2[1] - arrow_len * math.sin(angle + arrow_angle)
                    draw.polygon([p2, (x1, y1), (x2, y2)], fill="red", outline="white")

                # Draw numbered circular touch points
                for idx, (x, y) in enumerate(pts, 1):
                    # Outer aura
                    draw.ellipse(
                        [
                            x - dot_radius * 1.3,
                            y - dot_radius * 1.3,
                            x + dot_radius * 1.3,
                            y + dot_radius * 1.3,
                        ],
                        outline="white",
                        width=line_width + 1,
                    )
                    # Colored circle
                    draw.ellipse(
                        [x - dot_radius, y - dot_radius, x + dot_radius, y + dot_radius],
                        fill="red",
                        outline="white",
                        width=line_width,
                    )
                    label = str(idx)
                    try:
                        font = ImageFont.load_default(size=int(dot_radius * 1.2))
                        draw.text(
                            (x - dot_radius // 3, y - dot_radius // 2),
                            label,
                            fill="white",
                            font=font,
                            stroke_width=2,
                            stroke_fill="black",
                        )
                    except Exception:
                        draw.text(
                            (x - 4, y - 6),
                            label,
                            fill="white",
                        )

        # 3. Swipe / Drag / Scroll
        elif action in ("swipe", "drag", "scroll"):
            kind, target, _ = parse_swipe_parameters(action_args)
            start_pt, end_pt = None, None

            if kind == "coords" and isinstance(target, list) and len(target) == 4:
                start_pt = _resolve_coordinates(target[:2], width, height)
                end_pt = _resolve_coordinates(target[2:], width, height)
            elif kind == "direction" and isinstance(target, str):
                direction = target.lower()
                cx, cy = width // 2, height // 2
                dist_y = int(height * 0.35)
                dist_x = int(width * 0.35)
                if "up" in direction:
                    start_pt = (cx, cy + dist_y // 2)
                    end_pt = (cx, cy - dist_y // 2)
                elif "down" in direction:
                    start_pt = (cx, cy - dist_y // 2)
                    end_pt = (cx, cy + dist_y // 2)
                elif "left" in direction:
                    start_pt = (cx + dist_x // 2, cy)
                    end_pt = (cx - dist_x // 2, cy)
                elif "right" in direction:
                    start_pt = (cx - dist_x // 2, cy)
                    end_pt = (cx + dist_x // 2, cy)

            if start_pt and end_pt:
                sx, sy = start_pt
                ex, ey = end_pt

                draw.ellipse(
                    [
                        sx - dot_radius // 2,
                        sy - dot_radius // 2,
                        sx + dot_radius // 2,
                        sy + dot_radius // 2,
                    ],
                    fill="yellow",
                    outline="black",
                    width=2,
                )
                draw.line([(sx, sy), (ex, ey)], fill="white", width=line_width + 4)
                draw.line([(sx, sy), (ex, ey)], fill="red", width=line_width)

                dx = ex - sx
                dy = ey - sy
                angle = math.atan2(dy, dx)
                arrow_len = max(24, int(base_dim * 0.04))
                arrow_angle = math.pi / 6

                x1 = ex - arrow_len * math.cos(angle - arrow_angle)
                y1 = ey - arrow_len * math.sin(angle - arrow_angle)
                x2 = ex - arrow_len * math.cos(angle + arrow_angle)
                y2 = ey - arrow_len * math.sin(angle + arrow_angle)

                draw.polygon([(ex, ey), (x1, y1), (x2, y2)], fill="red", outline="white")

        # 4. Input Text
        elif action in ("input_text", "type"):
            coord = action_args.get("target") or action_args.get("coordinates")
            if coord:
                pt = _resolve_coordinates(coord, width, height)
                if pt:
                    x, y = pt
                    box_w = max(60, int(base_dim * 0.15))
                    box_h = max(30, int(base_dim * 0.06))
                    draw.rectangle(
                        [x - box_w, y - box_h, x + box_w, y + box_h],
                        outline="cyan",
                        width=line_width,
                    )

        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=85)
        return out_buf.getvalue()

    except Exception:
        return image_bytes


def overlay_action_on_screenshot(image_bytes: bytes, action_obj: Any) -> bytes | None:
    """Draws a recorded step action (as stored in ``action_taken``) onto the
    step's before-action screenshot.

    Accepts the raw stored action (a dict, or a burst list whose first member
    is used), maps the usual coordinate keys into the drawing arguments and
    returns the annotated JPEG bytes. Returns ``None`` when there is nothing to
    draw (no action, no coordinates, or the drawing left the image unchanged).
    Shared by the Checker's ``get_step_screenshot(which="overlay")`` and the
    MCP trace inspector so both produce the same picture.
    """
    if not image_bytes:
        return None
    action = action_obj[0] if isinstance(action_obj, list) and action_obj else action_obj
    if not isinstance(action, dict):
        return None
    act_name = action.get("action") or action.get("name") or ""
    if not act_name:
        return None
    action_args = dict(action.get("args") or {})
    for key in (
        "target",
        "coordinates",
        "point",
        "direction",
        "sequence",
        "targets",
        "normalized_coordinates",
    ):
        if key in action and key not in action_args:
            action_args[key] = action[key]
    annotated = draw_action_overlay_on_image(
        image_bytes=image_bytes, action_name=act_name, action_args=action_args
    )
    if not annotated or annotated == image_bytes:
        return None
    return annotated


@functools.lru_cache(maxsize=16)
def _som_font(fs: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", fs)
    except OSError:
        return ImageFont.load_default()


def select_som_elements(elements: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Selects elements eligible for selective Set-of-Mark (L-054):
    unlabeled controls (``is_icon``), input fields, and OCR gap-fill lines,
    never keyboard keys and never regular XML-labeled elements."""
    if not elements:
        return []
    selected: list[dict[str, Any]] = []
    for el in elements:
        if not isinstance(el, dict) or el.get("is_keyboard"):
            continue
        b = el.get("bounds")
        if not isinstance(b, (list, tuple)) or len(b) != 4:
            continue
        x1, y1, x2, y2 = int(b[0]), int(b[1]), int(b[2]), int(b[3])
        if x2 <= x1 or y2 <= y1:
            continue
        if el.get("index") is None:
            continue
        if el.get("is_icon") or el.get("is_ocr") or el.get("is_hint") or _is_input_element(el):
            selected.append(el)
    return selected


def draw_som_marks(
    image_bytes: bytes,
    elements: list[dict[str, Any]] | None,
    *,
    max_side: int = 0,
    jpeg_quality: int = 80,
) -> bytes:
    """Draws Set-of-Mark v2 (``som_image_v2``) index boxes onto ``image_bytes``
    at full resolution before optional downscaling to ``max_side`` (L-054)."""
    if not image_bytes:
        return image_bytes
    candidates = select_som_elements(elements)
    if not candidates:
        if max_side and max_side > 0:
            from artemis.utils.model_image import shrink_for_model

            return shrink_for_model(image_bytes, max_side, jpeg_quality)
        return image_bytes

    try:
        from PIL import Image, ImageDraw

        im = Image.open(io.BytesIO(image_bytes))
        if im.mode != "RGB":
            im = im.convert("RGB")
        W, H = im.size
        d = ImageDraw.Draw(im)
        fs = max(12, W // 55)
        font = _som_font(fs)

        def _clamped_bounds(el: dict[str, Any]) -> tuple[int, int, int, int] | None:
            x1, y1, x2, y2 = (int(v) for v in el["bounds"])
            x1, y1 = max(0, min(W, x1)), max(0, min(H, y1))
            x2, y2 = max(0, min(W, x2)), max(0, min(H, y2))
            return (x1, y1, x2, y2) if x2 > x1 and y2 > y1 else None

        norm_cands: list[tuple[dict[str, Any], tuple[int, int, int, int]]] = []
        for el in candidates:
            cb = _clamped_bounds(el)
            if cb is not None:
                norm_cands.append((el, cb))
        if not norm_cands:
            if max_side and max_side > 0:
                from artemis.utils.model_image import shrink_for_model

                return shrink_for_model(image_bytes, max_side, jpeg_quality)
            return image_bytes

        def _area(b: tuple[int, int, int, int]) -> int:
            return max(1, (b[2] - b[0]) * (b[3] - b[1]))

        marks: list[tuple[dict[str, Any], tuple[int, int, int, int]]] = []
        for el, b in sorted(norm_cands, key=lambda item: _area(item[1])):
            if any(
                (min(b[2], m[2]) - max(b[0], m[0])) > 0
                and (min(b[3], m[3]) - max(b[1], m[1])) > 0
                and (min(b[2], m[2]) - max(b[0], m[0])) * (min(b[3], m[3]) - max(b[1], m[1]))
                > 0.6 * min(_area(b), _area(m))
                for _, m in marks
            ):
                continue
            marks.append((el, b))

        lw = max(2, W // 500)
        for _, b in marks:
            d.rectangle(b, outline=(230, 20, 20), width=lw)

        placed: list[tuple[int, int, int, int]] = []

        def _free(r: tuple[int, int, int, int]) -> bool:
            return (
                r[0] >= 0
                and r[1] >= 0
                and r[2] <= W
                and r[3] <= H
                and not any(r[0] < q[2] and q[0] < r[2] and r[1] < q[3] and q[1] < r[3] for q in placed)
            )

        for el, b in marks:
            x1, y1, x2, y2 = b
            lab = str(el["index"])
            tw, th = int(d.textlength(lab, font=font)) + 6, fs + 4
            spots = [(x1, y1 - th), (x1 - tw, y1), (x1, y2), (x2, y1), (x1, y1)]
            r = next(
                ((sx, sy, sx + tw, sy + th) for sx, sy in spots if _free((sx, sy, sx + tw, sy + th))),
                (max(0, min(W - tw, x1)), max(0, min(H - th, y1)), max(0, min(W - tw, x1)) + tw, max(0, min(H - th, y1)) + th),
            )
            placed.append(r)
            d.rectangle(r, fill=(230, 20, 20), outline=(255, 255, 255), width=1)
            d.text((r[0] + 3, r[1] + 1), lab, fill=(255, 255, 255), font=font)

        if max_side and max_side > 0 and max(W, H) > max_side:
            scale = max_side / float(max(W, H))
            im = im.resize(
                (max(1, round(W * scale)), max(1, round(H * scale))),
                Image.Resampling.LANCZOS,
            )
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=int(jpeg_quality), optimize=True)
        return out.getvalue()
    except Exception as exc:
        logger.debug(f"Selective SoM rendering failed; falling back to unmarked image: {exc}")
        if max_side and max_side > 0:
            from artemis.utils.model_image import shrink_for_model

            return shrink_for_model(image_bytes, max_side, jpeg_quality)
        return image_bytes
