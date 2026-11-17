"""Snap a model-estimated tap point onto the center of the element it names.

Vision models estimate coordinates imprecisely, especially on tall screens: a
tap aimed at a bottom bar button can land a few percent above it, in the gap
between two controls. When a coordinate target carries a ``target_description``
that names an element present in the indexed UI list (by its own text), and the
estimated point is on or near that element, the tap is moved to the element's
center.

The snap is deliberately conservative: it only moves a tap toward an element
whose text matches the description, never toward an arbitrary neighbour, and it
skips relational descriptions ("plus button for <dish>") whose named text is a
label next to the real target rather than the target itself.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

#: Max distance between the estimated point and the element's box, as a fraction
#: of the screen height (0.06 of 2772 px ~ 166 px).
DEFAULT_MAX_DISTANCE_FRACTION = 0.06

#: Elements larger than this fraction of the screen are containers, not targets.
MAX_ELEMENT_AREA_FRACTION = 0.3

#: Generic words that describe a control's look or kind, not its identity.
_STOPWORDS = frozenset(
    {
        # English
        "button", "btn", "icon", "tab", "link", "label", "text", "field", "box", "bar",
        "the", "this", "that", "and", "with", "item", "items", "card", "area", "row",
        "green", "red", "blue", "white", "black", "gray", "grey", "orange", "yellow",
        "purple", "pink", "dark", "light", "big", "small", "large", "round", "circular",
        "top", "bottom", "left", "right", "center", "middle", "upper", "lower",
        "tap", "click", "press", "open", "select",
        # Vietnamese (accent-stripped)
        "nut", "bieu", "tuong", "mau", "xanh", "do", "trang", "den", "cai",
    }
)

#: Words naming a distinct control kind. A description using one ("plus button
#: for Pho") targets that control, so an element whose own text lacks the word
#: (the dish title) is a label, not the target.
_CONTROL_WORDS = frozenset(
    {"plus", "add", "minus", "remove", "close", "dismiss", "delete", "more", "menu",
     "back", "them", "bot", "xoa", "dong"}
)

#: Words marking a description as relational: the named text is a neighbour or
#: owner of the target ("plus button for X", "icon next to Y"), so snapping onto
#: that text would hit the wrong control.
_RELATIONAL = re.compile(
    r"\b(for|of|next to|beside|near|below|above|under|inside|on the (left|right))\b"
    r"|\b(cua|canh|ben|duoi|tren|gan)\b"
)


@dataclass(frozen=True)
class SnapResult:
    """A snapped tap: the new normalized point and the element it landed on."""

    point: list[int]
    element_text: str
    element_bounds: list[int]


def _fold(text: str) -> str:
    """Lowercase and strip diacritics ("Bánh mì" -> "banh mi", "đ" -> "d")."""
    text = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D"))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def _tokens(text: str) -> set[str]:
    return {
        tok
        for tok in re.findall(r"[a-z0-9]+", _fold(text))
        if len(tok) >= 2 and not tok.isdigit() and tok not in _STOPWORDS
    }


def _pixel_bounds(raw: Any) -> list[int] | None:
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            x1, y1, x2, y2 = (int(v) for v in raw)
        except (TypeError, ValueError):
            return None
    elif isinstance(raw, str):
        match = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", raw)
        if not match:
            return None
        x1, y1, x2, y2 = map(int, match.groups())
    else:
        return None
    if x1 >= x2 or y1 >= y2:
        return None
    return [x1, y1, x2, y2]


def snap_to_named_element(
    point: list[int],
    description: str | None,
    elements: list[dict[str, Any]] | None,
    width: int,
    height: int,
    *,
    max_distance_fraction: float = DEFAULT_MAX_DISTANCE_FRACTION,
) -> SnapResult | None:
    """Returns the snapped normalized point, or ``None`` to keep the original.

    ``point`` is normalized ``[x, y]`` (0-1000); element bounds are in pixels
    of a ``width`` x ``height`` screen.
    """
    if not description or not elements or width <= 0 or height <= 0:
        return None
    folded = _fold(description)
    if _RELATIONAL.search(folded):
        return None
    wanted = _tokens(description)
    if not wanted:
        return None
    controls = wanted & _CONTROL_WORDS

    px = point[0] * width / 1000.0
    py = point[1] * height / 1000.0
    max_dist = max_distance_fraction * height
    screen_area = float(width * height)

    best: tuple[float, float, dict[str, Any], list[int]] | None = None
    for element in elements:
        text = str(element.get("text") or "").strip()
        bounds = _pixel_bounds(element.get("bounds"))
        if not text or bounds is None:
            continue
        x1, y1, x2, y2 = bounds
        if (x2 - x1) * (y2 - y1) > MAX_ELEMENT_AREA_FRACTION * screen_area:
            continue
        own = _tokens(text)
        if not own or not controls <= own:
            continue
        shared = wanted & own
        # Most of what the description names must be this element's own text,
        # and most of the element's text must be named: a long label that merely
        # shares a word or two with the description is a neighbour, not the target.
        coverage = len(shared) / len(wanted)
        if not shared or coverage < 0.5 or len(shared) / len(own) < 0.5:
            continue
        dx = max(x1 - px, 0.0, px - x2)
        dy = max(y1 - py, 0.0, py - y2)
        distance = (dx * dx + dy * dy) ** 0.5
        if distance > max_dist:
            continue
        key = (-coverage, distance)
        if best is None or key < (best[0], best[1]):
            best = (-coverage, distance, element, bounds)

    if best is None:
        return None
    _, _, element, (x1, y1, x2, y2) = best
    cx = round((x1 + x2) / 2 * 1000 / width)
    cy = round((y1 + y2) / 2 * 1000 / height)
    norm_bounds = [
        round(x1 * 1000 / width),
        round(y1 * 1000 / height),
        round(x2 * 1000 / width),
        round(y2 * 1000 / height),
    ]
    return SnapResult(
        point=[max(0, min(1000, cx)), max(0, min(1000, cy))],
        element_text=str(element.get("text") or "").strip(),
        element_bounds=norm_bounds,
    )
