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
import io
import os
import re
from typing import Any

from artemis.utils.coordinates import parse_swipe_parameters
from PIL import Image, ImageDraw, ImageFont


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
    for res in ocr_results or []:
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


def _is_status_bar_node(node: dict[str, Any], height: int, bounds: tuple[int, int, int, int] | None) -> bool:
    """A system UI node drawn inside the top status-bar band."""
    if not bounds or str(node.get("package") or "") != "com.android.systemui":
        return False
    return bounds[3] <= height * _STATUS_BAR_FRACTION


def _icon_kind(node: dict[str, Any]) -> str:
    """Short role word for an unlabeled control, from its Android class."""
    cls = str(node.get("class") or "")
    if "EditText" in cls:
        return "input"
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
    required_ids, star_ids = (
        _required_markers(fused_xml, width) if compact and mark_required else (set(), set())
    )

    def is_duplicate(text_val: str, cx: int, cy: int) -> bool:
        text_clean = text_val.strip()
        for ex_text, ex_cx, ex_cy in added_items:
            if ex_text == text_clean:
                dist = ((cx - ex_cx) ** 2 + (cy - ex_cy) ** 2) ** 0.5
                if dist < 8.0:
                    return True
        return False

    for node in fused_xml:
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
            if compact:
                prefix = "" if text.strip() else "hint "
                line = f"[{idx}] {prefix}{quote(shown)} {norm_bounds}"
                # Only informative state: ON/OFF of a checkable control (#187),
                # and a disabled control. Defaults (enabled, unchecked) are noise.
                if checkable or str(node.get("checked") or "").strip().lower() == "true":
                    line += f" checked:{state_token(node.get('checked'))}"
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
            elif checkable or node.get("checked") is not None:
                line += " | " + " ".join(
                    f"{a}:{state_token(node.get(a))}" for a in ("checked", "enabled")
                )
            elif str(node.get("enabled") or "").strip().lower() == "false":
                line += " | enabled:false"
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
            # Raw uiautomator state attributes, carried on the element for
            # code-side verification (signatures, state predicates).
            for state_attr in ("checked", "enabled", "selected", "package"):
                if node.get(state_attr) is not None:
                    element[state_attr] = node.get(state_attr)
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
            fused_xml, width, height, elements, start_idx=idx, normalize_bounds=normalize_bounds
        )
        lines.extend(icon_lines)
        elements.extend(icon_elements)
        labels.extend(str(el["index"]) for el in icon_elements)
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
) -> tuple[list[str], list[dict[str, Any]]]:
    """Clickable controls with no text, hint or description (back arrows, "+",
    search fields, toggles), as compact lines plus element records.

    Skipped: containers (too large, or holding an already listed element's
    center), slivers, and a wrapper that repeats the center of a control
    already taken.
    """
    lines: list[str] = []
    found: list[dict[str, Any]] = []
    screen_area = max(1, width * height)
    listed_centers = [el["center"] for el in listed if el.get("center")]
    candidates: list[tuple[int, dict[str, Any]]] = []
    for pos, node in enumerate(fused_xml):
        if len(candidates) >= _MAX_ICON_LINES:
            break
        if (node.get("text") or node.get("content-desc") or node.get("hint") or "").strip():
            continue
        if node.get("ocr_elements") or node.get("ocr_virtual"):
            continue
        cls = str(node.get("class") or "")
        if not (_is_truthy(node.get("clickable")) or _is_truthy(node.get("long-clickable")) or "EditText" in cls):
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
        cap = _INPUT_MAX_AREA_FRACTION if "EditText" in cls else _ICON_MAX_AREA_FRACTION
        if w * h > screen_area * cap:
            continue
        if _is_status_bar_node(node, height, (left, top, right, bottom)):
            continue
        if any(left <= cx <= right and top <= cy <= bottom for cx, cy in listed_centers):
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
        for state_attr in ("checked", "enabled", "selected", "package"):
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
