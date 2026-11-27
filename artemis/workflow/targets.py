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

"""Target descriptors for the app-workflow autopilot (KE_HOACH_APP_WORKFLOW Mục 3.4).

Everything works on ``fingerprint.normalize`` nodes (0-1000 canvas). A recorded
target never stores an absolute point: it is a descriptor (resource id, static
text, class + grid cell, or the slot text) that is resolved again on the live
screen, and must resolve to exactly one visible, unoccluded, non-commitment node.
No app name or button wording lives here (AGENTS.md 2.7).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re
from typing import Any, Iterable

from artemis.utils.screen_diff import flatten_hierarchy
from artemis.workflow.fingerprint import SYSTEMUI_PACKAGE, foreground_package, normalize

# Containers covering > 40% of the canvas are layout wrappers, never targets.
MAX_TARGET_AREA_NORM = 400_000

Box = tuple[int, int, int, int]


def area(box: Box) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def intersection(a: Box, b: Box) -> int:
    l, t = max(a[0], b[0]), max(a[1], b[1])
    r, btm = min(a[2], b[2]), min(a[3], b[3])
    return (r - l) * (btm - t) if r > l and btm > t else 0


def iou(a: Box, b: Box) -> float:
    inter = intersection(a, b)
    if inter <= 0:
        return 0.0
    union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0


def center(box: Box) -> tuple[int, int]:
    return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2


def grid_of(box: Box) -> tuple[int, int]:
    cx, cy = center(box)
    return min(9, max(0, cx // 100)), min(9, max(0, cy // 100))


def _box(node: dict[str, Any]) -> Box:
    b = node.get("bounds") or (0, 0, 0, 0)
    return (int(b[0]), int(b[1]), int(b[2]), int(b[3]))


def fold(text: str) -> str:
    """Case- and accent-insensitive form ("Reykjavík" == "reykjavik", "Phở" == "pho")."""
    import unicodedata

    t = unicodedata.normalize("NFKD", (text or "").replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in t if not unicodedata.combining(c)).casefold().strip()


_FIELD_NOISE_RE = re.compile(r"[\s\-().]+")
_THOUSANDS_NUM_RE = re.compile(r"^\s*[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*$")
_PASSWORD_MASK_CHARS = frozenset("•*●◦▪▫∙")


def normalize_field_value(text: str) -> str:
    """Canonical field-value comparison key: keeps accents (NFC), casefolds,
    strips whitespace and '-', '(', ')', '.', and ignores thousands ',' when the
    whole value is numeric (L-045, L-049, L-055)."""
    import unicodedata

    nfc = unicodedata.normalize("NFC", text or "")
    if _THOUSANDS_NUM_RE.match(nfc):
        nfc = nfc.replace(",", "")
    return _FIELD_NOISE_RE.sub("", nfc).casefold()


_DOUBLE_QUOTED_RE = re.compile(r'"([^"\n]{2,80})"|“([^”\n]{2,80})”')
_SINGLE_QUOTED_RE = re.compile(r"(?<!\w)'([^'\n]{2,80})'(?!\w)")
_EMAIL_LITERAL_RE = re.compile(r"(?<!\w)([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})(?!\w)")
_NUMBER_LITERAL_RE = re.compile(
    r"(?<!\w)(\d[\d,]{1,}\d|\d{3,})(?!\w)(?!\s*(?:ms|s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|giay|giây|phut|phút|gio|giờ|px|dp|%)\b)",
    re.IGNORECASE,
)


def extract_requested_literals(goal: str, *, include_quoted: bool = False) -> list[str]:
    """Extracts explicit data literals (emails, >=3-digit numbers that are not
    duration/size units; quoted strings only with ``include_quoted``) from a task
    goal, in order of appearance (L-057, L-058, L-059).

    Quoted strings are excluded by default (L-061): goals quote UI labels and
    instructions as often as data ("do not tap 'Place Order'", confirm "Yes"),
    so requiring them on the final screen would push the model toward the very
    control the task forbids. Quoted values the run actually types are tracked
    from the typing history instead."""
    text = str(goal or "").strip()
    if not text:
        return []
    candidates: list[tuple[int, str]] = []
    for m in (_DOUBLE_QUOTED_RE.finditer(text) if include_quoted else ()):
        val = (m.group(1) or m.group(2) or "").strip()
        if val:
            candidates.append((m.start(), val))
    for m in (_SINGLE_QUOTED_RE.finditer(text) if include_quoted else ()):
        val = m.group(1).strip()
        if val:
            candidates.append((m.start(), val))
    for m in _EMAIL_LITERAL_RE.finditer(text):
        val = m.group(1).strip()
        if val:
            candidates.append((m.start(), val))
    for m in _NUMBER_LITERAL_RE.finditer(text):
        val = m.group(1).strip().strip(",")
        if sum(ch.isdigit() for ch in val) >= 3:
            candidates.append((m.start(), val))

    candidates.sort(key=lambda item: item[0])
    out: list[str] = []
    seen_norms: list[str] = []
    for _, val in candidates:
        norm = normalize_field_value(val)
        if not norm:
            continue
        if any(norm == s or norm in s for s in seen_norms):
            continue
        seen_norms.append(norm)
        out.append(val)
    return out


def check_field_value(node: dict[str, Any], expected_text: str) -> tuple[bool, bool, str]:
    """Compares a live editable node's content against ``expected_text`` (L-045, L-049).

    Returns ``(matched, verifiable, actual_display)``:
    - Normal fields: compares ``normalize_field_value`` (case-insensitive,
      whitespace/`-().` ignored, accents preserved). ``verifiable`` is False when
      empty or still only showing its placeholder hint.
    - ``password=True`` fields: never compares text content (Android masks it as
      '••••' or hides it). If all characters in the field are mask glyphs,
      checks masked length == ``len(expected_text.strip())`` (``verifiable=True``);
      otherwise returns ``(False, False, "")`` so no false mismatch is reported.
    """
    expected = str(expected_text or "").strip()
    actual = str(node.get("text") or "").strip()
    if not expected:
        return False, False, actual
    if node.get("password"):
        if actual and all(ch in _PASSWORD_MASK_CHARS for ch in actual):
            return len(actual) == len(expected), True, actual
        return False, False, ""
    if not actual:
        return False, False, ""
    hint_raw = str(node.get("hint") or "").strip()
    if hint_raw and fold(actual) == fold(hint_raw) and normalize_field_value(actual) != normalize_field_value(expected):
        return False, False, ""
    return normalize_field_value(actual) == normalize_field_value(expected), True, actual


def slot_parts(slot: str) -> tuple[str, ...]:
    """Splits a single or composite slot key (``'slot1+slot2'`` -> ``('slot1', 'slot2')``) (L-047)."""
    return tuple(p for p in (slot or "").split("+") if p)


def resolve_slot_value(slot: str, slot_values: dict[str, str]) -> str:
    """Resolves a single or composite slot key into the string value to type or display (L-047)."""
    parts = slot_parts(slot)
    if not parts:
        return ""
    vals = [str(slot_values.get(p) or "").strip() for p in parts]
    if any(not v for v in vals):
        return ""
    return " ".join(vals)


_LIST_CONTAINER_CLASSES = ("RecyclerView", "ListView", "GridView")


def _inside_list_container(node: dict[str, Any], norm: list[dict[str, Any]]) -> bool:
    if node.get("in_scrollable"):
        return True
    nb = _box(node)
    cx, cy = center(nb)
    pkg = node.get("package") or ""
    for other in visible_app_nodes(norm):
        if other is node or (pkg and other.get("package") != pkg):
            continue
        cls = str(other.get("class") or "")
        if any(c in cls for c in _LIST_CONTAINER_CLASSES):
            ob = _box(other)
            if area(ob) >= 1000 and ob[0] <= cx <= ob[2] and ob[1] <= cy <= ob[3]:
                if nb[0] >= ob[0] - 5 and nb[1] >= ob[1] - 5 and nb[2] <= ob[2] + 5 and nb[3] <= ob[3] + 5:
                    return True
    return False


def _same_kind(node: dict[str, Any], visible: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Visible entries of the same kind as ``node``: same resource id, or for
    unnamed nodes same class and width (or aligned list edge inside a list
    container) at another height."""
    rid = node.get("resource-id") or ""
    b = _box(node)
    if rid:
        return [n for n in visible if n.get("resource-id") == rid and node_label(n)]
    width = b[2] - b[0]
    in_list = _inside_list_container(node, visible)
    out: list[dict[str, Any]] = []
    for n in visible:
        if n.get("class") != node.get("class") or not node_label(n):
            continue
        nb = _box(n)
        same_width = abs((nb[2] - nb[0]) - width) <= 10
        aligned_edge = in_list and _inside_list_container(n, visible) and (
            abs(nb[0] - b[0]) <= 20 or abs(nb[2] - b[2]) <= 20
        )
        if same_width or aligned_edge:
            out.append(n)
    return out


def node_label(node: dict[str, Any]) -> str:
    return str(node.get("text") or node.get("content-desc") or "").strip()


def is_data_label(label: str) -> bool:
    """A label made only of digits, punctuation, symbols and spaces ("2", "111.000",
    "+2h" is not: it has a letter). Such a node shows data (a quantity, a price, a
    count) that depends on the state of the account, not a navigation control:
    the autopilot never taps it on its own (L-027). Decided by Unicode category,
    no currency or wording list."""
    import unicodedata

    text = (label or "").strip()
    if not text or not any(ch.isdigit() for ch in text):
        return False
    return all(unicodedata.category(ch)[0] in ("N", "P", "S", "Z") for ch in text)


def visible_app_nodes(norm: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in norm:
        if node.get("package") == SYSTEMUI_PACKAGE or not node.get("visible-to-user", True):
            continue
        if area(_box(node)) <= 0:
            continue
        out.append(node)
    return out


def is_occluded(target: dict[str, Any], visible: list[dict[str, Any]]) -> bool:
    """A node drawn later (not inside the target) covering >= 50% of it."""
    try:
        idx = next(i for i, n in enumerate(visible) if n is target)
    except StopIteration:
        return False
    tb = _box(target)
    t_area = max(1, area(tb))
    pkg = target.get("package")
    for later in visible[idx + 1 :]:
        # Dump order is z-order only inside one window: another package (the
        # keyboard, the app under it) is not drawn over by list position.
        if later.get("package") != pkg:
            continue
        lb = _box(later)
        if area(lb) > MAX_TARGET_AREA_NORM and not later.get("clickable") and not node_label(later):
            continue  # transparent layout root, not a covering view
        if lb[0] >= tb[0] - 2 and lb[1] >= tb[1] - 2 and lb[2] <= tb[2] + 2 and lb[3] <= tb[3] + 2:
            continue
        if intersection(tb, lb) / t_area >= 0.5:
            return True
    return False


def _commitment(text: str) -> bool:
    from artemis.mcp.action_executor import is_commitment_text

    return bool(text) and is_commitment_text(text)


def is_commitment_at(norm: list[dict[str, Any]], box: Box, extra_texts: Iterable[str] = ()) -> bool:
    """Commitment wording on the target, the clickable node holding its center,
    or any labelled node inside / overlapping it (KE_HOACH Mục 5.1)."""
    if any(_commitment(t) for t in extra_texts if t):
        return True
    cx, cy = center(box)
    for node in visible_app_nodes(norm):
        nb = _box(node)
        holds_center = nb[0] <= cx <= nb[2] and nb[1] <= cy <= nb[3]
        overlaps = iou(nb, box) >= 0.5
        inside = nb[0] >= box[0] - 5 and nb[1] >= box[1] - 5 and nb[2] <= box[2] + 5 and nb[3] <= box[3] + 5
        if (holds_center and node.get("clickable")) or overlaps or inside:
            for txt in (node.get("text") or "", node.get("content-desc") or ""):
                if _commitment(txt):
                    return True
    return False


def node_at(
    norm: list[dict[str, Any]],
    *,
    point: tuple[int, int] | None = None,
    box: Box | None = None,
    resource_id: str = "",
) -> dict[str, Any] | None:
    """The node an action landed on: best IoU with ``box`` when known, else the
    smallest (clickable-preferred) node holding ``point``. Oversized layout
    containers are never returned."""
    cands = [n for n in visible_app_nodes(norm) if area(_box(n)) <= MAX_TARGET_AREA_NORM]
    if box is not None:
        scored = [
            (iou(box, _box(n)), int(bool(resource_id and n.get("resource-id") == resource_id)), -area(_box(n)), i)
            for i, n in enumerate(cands)
            if iou(box, _box(n)) >= 0.5
        ]
        if scored:
            return cands[max(scored)[3]]
    if point is not None:
        px, py = point
        scored2 = []
        for i, n in enumerate(cands):
            nb = _box(n)
            if nb[0] <= px <= nb[2] and nb[1] <= py <= nb[3]:
                scored2.append(
                    (
                        int(bool(resource_id and n.get("resource-id") == resource_id)),
                        int(bool(n.get("clickable") or n.get("editable"))),
                        -area(nb),
                        i,
                    )
                )
        if scored2:
            return cands[max(scored2)[3]]
    return None


@dataclass
class TargetDescriptor:
    """Coordinate-free description of the element an edge acts on."""

    resource_id: str = ""
    class_name: str = ""
    text: str = ""
    grid: tuple[int, int] = (0, 0)
    slot: str = ""  # slot name: resolve by the slot's value instead of ``text``
    hint: str = ""  # an input field's accessibility hint (fields often lack ids)
    focused_editable: bool = False  # resolve to the single focused editable field

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["grid"] = list(self.grid)
        return {k: v for k, v in d.items() if v not in ("", False)}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "TargetDescriptor":
        d = dict(d or {})
        grid = d.get("grid") or (0, 0)
        return cls(
            resource_id=str(d.get("resource_id") or ""),
            class_name=str(d.get("class_name") or ""),
            text=str(d.get("text") or ""),
            grid=(int(grid[0]), int(grid[1])),
            slot=str(d.get("slot") or ""),
            hint=str(d.get("hint") or ""),
            focused_editable=bool(d.get("focused_editable")),
        )


def descriptor_for(node: dict[str, Any], *, slot: str = "") -> TargetDescriptor:
    return TargetDescriptor(
        resource_id=str(node.get("resource-id") or ""),
        class_name=str(node.get("class") or ""),
        text="" if slot else node_label(node),
        grid=grid_of(_box(node)),
        slot=slot,
        hint=str(node.get("hint") or "") if is_editable(node) else "",
    )


def is_editable(node: dict[str, Any]) -> bool:
    cls = str(node.get("class") or "")
    return bool(node.get("editable")) or "EditText" in cls or "AutoCompleteTextView" in cls


def resolve(
    desc: TargetDescriptor,
    norm: list[dict[str, Any]],
    *,
    slot_value: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Resolves ``desc`` on a live screen. Status ``ok`` only for exactly one
    visible, unoccluded, non-oversized, non-commitment node."""
    visible = visible_app_nodes(norm)
    matches: list[dict[str, Any]] = []

    if desc.focused_editable:
        matches = [n for n in visible if n.get("focused") and is_editable(n)]
        if len(matches) != 1:
            return None, "no_focused_field" if not matches else "ambiguous_focus"
    elif desc.slot:
        value = fold(slot_value or "")
        if not value:
            return None, "slot_missing"
        # The field the value was typed into also shows it: never a pick target.
        matches = [n for n in visible if value in fold(node_label(n)) and not is_editable(n)]
        if len(matches) > 1:
            return None, "ambiguous_slot"
        if len(matches) == 1 and len(_same_kind(matches[0], visible)) > 1:
            # Several entries of the same kind are listed (other results,
            # variants): picking one is a choice for the AI / the user, unless
            # it is a non-scrollable static option/tab matching the exact slot value.
            if matches[0].get("in_scrollable") or fold(node_label(matches[0])) != value:
                return None, "list_choice"
    else:
        if desc.resource_id:
            rid = [n for n in visible if n.get("resource-id") == desc.resource_id]
            if len(rid) > 1:
                rid = [n for n in rid if grid_of(_box(n)) == desc.grid]
                if len(rid) > 1 and desc.text:
                    rid = [n for n in rid if node_label(n) == desc.text]
            if len(rid) > 1:
                return None, "ambiguous_rid"
            matches = rid
        if not matches and desc.hint:
            hinted = [n for n in visible if n.get("hint") == desc.hint and is_editable(n)]
            if len(hinted) > 1:
                return None, "ambiguous_hint"
            matches = hinted
        if not matches and desc.text:
            txt = [n for n in visible if desc.text in (n.get("text"), n.get("content-desc"))]
            if len(txt) > 1:
                txt = [n for n in txt if grid_of(_box(n)) == desc.grid]
            if len(txt) > 1:
                return None, "ambiguous_text"
            matches = txt
        if not matches and desc.class_name and not desc.text and not desc.hint:
            # Unlabelled icon: only when it is the single actionable node of
            # that class in the recorded grid cell.
            matches = [
                n
                for n in visible
                if (n.get("clickable") or is_editable(n))
                and n.get("class") == desc.class_name
                and grid_of(_box(n)) == desc.grid
            ]
            if len(matches) > 1:
                return None, "ambiguous_icon"

    if len(matches) != 1:
        return None, "not_found"
    node = matches[0]
    nb = _box(node)
    if area(nb) > MAX_TARGET_AREA_NORM:
        return None, "oversized_container"
    if is_occluded(node, visible) or covered_by_other_window(node, norm):
        return None, "occluded"
    if is_commitment_at(norm, nb, extra_texts=(node_label(node),)):
        return None, "commitment"
    if not desc.focused_editable and not is_editable(node) and is_data_label(node_label(node)):
        # A quantity / price / count: state-dependent data, never auto-tapped (L-027).
        return None, "data_label"
    return node, "ok"


def anchors_for(
    post: list[dict[str, Any]],
    pre: list[dict[str, Any]] | None,
    *,
    exclude_texts: Iterable[str] = (),
    limit: int = 3,
) -> list[dict[str, str]]:
    """Elements that identify the destination screen: present on ``post``,
    absent from ``pre``, outside scrolling content (lists change run to run),
    never carrying a slot value. Resource ids first, then short static labels."""
    excl = [e.casefold() for e in exclude_texts if e]
    pre_rids = {n.get("resource-id") for n in (pre or []) if n.get("resource-id")}
    pre_txt = {node_label(n) for n in (pre or []) if node_label(n)}
    rids: list[dict[str, str]] = []
    texts: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    from artemis.workflow.fingerprint import foreground_package

    fg = foreground_package(post)
    for n in visible_app_nodes(post):
        if n.get("in_scrollable") or (fg and n.get("package") != fg):
            continue
        rid = str(n.get("resource-id") or "")
        label = node_label(n)
        if label and any(e in label.casefold() for e in excl):
            continue
        if rid and rid not in pre_rids and ("rid", rid) not in seen:
            seen.add(("rid", rid))
            rids.append({"resource_id": rid})
        elif (
            label
            and len(label) <= 40
            and not any(ch.isdigit() for ch in label)  # clocks, prices, counts change
            and label not in pre_txt
            and ("t", label) not in seen
        ):
            seen.add(("t", label))
            texts.append({"text": label})
    return (rids + texts)[:limit]


def anchors_present(anchors: list[dict[str, str]], norm: list[dict[str, Any]]) -> bool:
    """True when no anchors were recorded, or at least one is on screen."""
    if not anchors:
        return True
    visible = visible_app_nodes(norm)
    rids = {n.get("resource-id") for n in visible}
    labels = {node_label(n) for n in visible}
    return any(
        (a.get("resource_id") and a["resource_id"] in rids) or (a.get("text") and a["text"] in labels)
        for a in anchors
    )


def typed_slots(norm: list[dict[str, Any]], slot_values: dict[str, str]) -> set[str]:
    """Slots whose value an input field on screen already holds. Typing does not
    change a screen's structure, so this tells "before typing" from "typed, now
    submit" on the same node."""
    fields = [fold(str(n.get("text") or "")) for n in visible_app_nodes(norm) if is_editable(n)]
    return {name for name, value in slot_values.items() if value and any(fold(value) in f for f in fields)}


def field_has_value(node: dict[str, Any], target_hint: str = "") -> bool:
    """True when an editable node currently holds user/existing content rather than
    being empty or only displaying its placeholder hint (L-034)."""
    text = str(node.get("text") or "").strip()
    if not text:
        return False
    hint = str(node.get("hint") or target_hint or "").strip()
    if hint and fold(text) == fold(hint):
        return False
    return True


def editable_field_keys(norm: Iterable[dict[str, Any]]) -> set[tuple[str, str]]:
    """Identifying (resource_id, hint) pairs of visible foreground editable fields."""
    nodes = list(norm)
    fg = foreground_package(nodes)
    keys: set[tuple[str, str]] = set()
    for n in visible_app_nodes(nodes):
        if fg and n.get("package") != fg:
            continue
        if not is_editable(n):
            continue
        rid = str(n.get("resource-id") or "").strip()
        hint = str(n.get("hint") or "").strip()
        if rid or hint:
            keys.add((rid, hint))
    return keys


def field_target_matches(target: TargetDescriptor | None, field_keys: set[tuple[str, str]]) -> bool:
    """True when ``target``'s resource-id or hint matches a field in ``field_keys``."""
    if target is None or not field_keys:
        return False
    rid = target.resource_id.strip()
    hint = target.hint.strip()
    if not (rid or hint):
        return False
    for k_rid, k_hint in field_keys:
        if rid and k_rid and k_rid != rid:
            continue
        if hint and k_hint and k_hint != hint:
            continue
        if (rid and k_rid == rid) or (hint and k_hint == hint):
            return True
    return False


def is_list_item(node: dict[str, Any], norm: list[dict[str, Any]]) -> bool:
    """A repeated entry of list/scrolling content (a result, a product, a contact),
    not a control that merely floats over the list (FAB, sticky button).
    Repetition is the pattern: another visible node shares its resource id, or
    (unnamed ids) its class and width / aligned left-or-right list edge at another height."""
    if not _inside_list_container(node, norm):
        return False
    box = _box(node)
    rid = node.get("resource-id") or ""
    width = box[2] - box[0]
    for other in visible_app_nodes(norm):
        if other is node:
            continue
        ob = _box(other)
        if rid:
            if other.get("resource-id") == rid and ob != box:
                return True
        elif (
            other.get("class") == node.get("class")
            and abs(ob[1] - box[1]) >= 20
            and _inside_list_container(other, norm)
            and (
                abs((ob[2] - ob[0]) - width) <= 10
                or abs(ob[0] - box[0]) <= 20
                or abs(ob[2] - box[2]) <= 20
            )
        ):
            return True
    return False


def covered_by_other_window(target: dict[str, Any], norm: list[dict[str, Any]]) -> bool:
    """A foreground-app target with another window's content over it (keyboard,
    dialog, bubble). Windows of other packages draw above the app, whatever
    their position in the dump; their transparent layout roots do not count."""
    from artemis.workflow.fingerprint import foreground_package

    fg = foreground_package(norm)
    if not fg or target.get("package") != fg:
        return False  # the target itself sits in an upper window (e.g. a keyboard key)
    tb = _box(target)
    t_area = max(1, area(tb))
    for n in visible_app_nodes(norm):
        if n.get("package") in (fg, None, ""):
            continue
        nb = _box(n)
        if area(nb) > MAX_TARGET_AREA_NORM and not n.get("clickable") and not node_label(n):
            continue
        if intersection(tb, nb) / t_area >= 0.5:
            return True
    return False


def keyboard_covers(target: dict[str, Any], norm: list[dict[str, Any]]) -> bool:
    """The app target is under an input-method-like window: another package's
    window spanning the screen width in the lower part, while an app field has
    focus. Pressing BACK hides it (standard Android behaviour)."""
    from artemis.workflow.fingerprint import foreground_package

    if not covered_by_other_window(target, norm):
        return False
    return keyboard_box(norm) is not None


def keyboard_box(norm: list[dict[str, Any]]) -> Box | None:
    """Box of an input-method-like window, or None: another package's window
    spanning the screen width in the lower part while an app field has focus
    (the soft keyboard; BACK hides it)."""
    from artemis.workflow.fingerprint import foreground_package

    fg = foreground_package(norm)
    if not any(n.get("focused") and is_editable(n) and n.get("package") == fg for n in visible_app_nodes(norm)):
        return None
    # A keyboard dump may have no full-width root, only its keys: use the
    # union of the other window's nodes in the lower part of the screen.
    boxes = [
        _box(n)
        for n in visible_app_nodes(norm)
        if n.get("package") not in (fg, None, "", SYSTEMUI_PACKAGE) and _box(n)[1] >= 400
    ]
    if len(boxes) < 5 and not any(b[2] - b[0] >= 900 for b in boxes):
        return None
    if not boxes:
        return None
    union = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    return union if union[2] - union[0] >= 800 else None


def other_window_boxes(norm: list[dict[str, Any]]) -> list[Box]:
    """Boxes of content drawn by other packages (keyboard keys, bubbles, dialogs)."""
    from artemis.workflow.fingerprint import foreground_package

    fg = foreground_package(norm)
    return [
        _box(n)
        for n in visible_app_nodes(norm)
        if n.get("package") not in (fg, None, "", SYSTEMUI_PACKAGE)
    ]


def screen_content_key(raw: Any) -> str | None:
    """Hash of the foreground app's visible labels AND element states (checked,
    selected, enabled, error): picking an option changes the key even when no
    text changes. Accepts either a ``dict`` or an object with ``.elements``.
    """
    if isinstance(raw, dict):
        elements = raw.get("elements")
        width = raw.get("width")
        height = raw.get("height")
    else:
        elements = getattr(raw, "elements", None)
        width = getattr(raw, "width", None)
        height = getattr(raw, "height", None)
    if not elements:
        return None
    norm = normalize(elements, width=width, height=height)
    fg = foreground_package(norm)
    labels = sorted(
        node_label(n) for n in visible_app_nodes(norm) if node_label(n) and (not fg or n.get("package") == fg)
    )
    if not labels:
        return None
    states = []
    for n in flatten_hierarchy(elements) or []:
        if not isinstance(n, dict) or (fg and n.get("package") not in (fg, None, "")):
            continue
        flags = "".join(
            k[0] for k in ("checked", "selected", "error") if str(n.get(k) or "").lower() not in ("", "false", "none", "0")
        )
        if str(n.get("enabled", "true")).lower() == "false":
            flags += "d"
        if flags:
            states.append(f"{n.get('resource-id') or n.get('text') or n.get('bounds')}:{flags}")
    body = "\x1f".join([fg] + labels + ["#"] + sorted(states))
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


_SCROLL_CONTAINER_HINTS = ("scrollview", "recyclerview", "listview", "webview", "viewpager", "gridview")


def _in_box(px: int, py: int, box: Box, margin: int = 15) -> bool:
    return (box[0] - margin) <= px <= (box[2] + margin) and (box[1] - margin) <= py <= (box[3] + margin)


def safe_swipe_in_norm(
    norm: list[dict[str, Any]],
    *,
    direction: str = "up",
    target_bounds: Box | None = None,
    indexed_elements: list[dict[str, Any]] | None = None,
    screen_width: int = 1000,
    screen_height: int = 1000,
    min_container_area: int = 150_000,
) -> list[int] | None:
    """Computes a safe swipe ``[sx, sy, ex, ey]`` (0-1000 scale) inside the
    foreground app's main scrollable container, avoiding other windows (keyboard,
    dialogs) and non-scrolling clickable/editable controls (FABs, bottom bars).
    Returns ``None`` when no safe band exists.
    """
    fg = foreground_package(norm)
    vis = visible_app_nodes(norm)
    if target_bounds is not None:
        l, t, r, b = target_bounds
    else:
        areas: list[Box] = []
        for n in vis:
            if fg and n.get("package") != fg:
                continue
            cls = str(n.get("class") or "").lower()
            is_scroll = bool(n.get("scrollable")) or any(k in cls for k in _SCROLL_CONTAINER_HINTS)
            if not is_scroll:
                continue
            nb = _box(n)
            if area(nb) >= min_container_area:
                areas.append(nb)
        if not areas:
            return None
        l, t, r, b = max(areas, key=area)

    # Clip out other-window overlays (keyboard, floating dialogs).
    for ol, ot, orr, ob in other_window_boxes(norm):
        if ol < r and orr > l and ot < b and ob > t:
            if ot > t:
                b = min(b, ot)
            elif ob < b:
                t = max(t, ob)
            else:
                return None

    dir_str = str(direction or "up").strip().lower()
    if dir_str in ("up", "down") and (b - t) < 200:
        return None
    if dir_str in ("left", "right") and (r - l) < 200:
        return None

    # Obstacles: any interactive control (area <= 250_000) for primary pass,
    # and strictly non-list / button / input controls for fallback pass when
    # a screen is filled edge-to-edge with clickable list cards.
    all_obstacles: list[Box] = []
    strict_obstacles: list[Box] = []
    for n in vis:
        if fg and n.get("package") != fg:
            continue
        if not (n.get("clickable") or is_editable(n) or n.get("checkable")):
            continue
        nb = _box(n)
        if not (0 < area(nb) <= 250_000):
            continue
        all_obstacles.append(nb)
        cls_l = str(n.get("class") or "").lower()
        if not n.get("in_scrollable") or "button" in cls_l or is_editable(n) or n.get("checkable"):
            strict_obstacles.append(nb)

    if not all_obstacles and indexed_elements:
        sw = max(1, int(screen_width or 1000))
        sh = max(1, int(screen_height or 1000))
        for elem in indexed_elements:
            eb = elem.get("bounds")
            if isinstance(eb, (list, tuple)) and len(eb) == 4:
                nb = (
                    round(int(eb[0]) * 1000 / sw),
                    round(int(eb[1]) * 1000 / sh),
                    round(int(eb[2]) * 1000 / sw),
                    round(int(eb[3]) * 1000 / sh),
                )
                if 0 < area(nb) <= 150_000 and (nb[1] >= 600 or nb[3] <= 200):
                    all_obstacles.append(nb)
                    strict_obstacles.append(nb)

    # Trim bottom/top full-width sticky bars overlapping the scrollable area.
    cw = r - l
    for ob_l, ob_t, ob_r, ob_b in strict_obstacles:
        if ob_l < r and ob_r > l and ob_t < b and ob_b > t and (ob_r - ob_l) >= int(cw * 0.7):
            if ob_t >= (t + b) // 2:
                b = min(b, ob_t - 10)
            elif ob_b <= (t + b) // 2:
                t = max(t, ob_b + 10)

    if dir_str in ("up", "down") and (b - t) < 200:
        return None
    if dir_str in ("left", "right") and (r - l) < 200:
        return None

    w = r - l
    h = b - t
    if dir_str in ("up", "down"):
        x_cands = [int(l + w * f) for f in (0.60, 0.20, 0.82, 0.35, 0.72, 0.50)]
        y_hi = b - max(20, h // 6)
        y_lo = t + max(20, h // 6)
        if dir_str == "up":
            y_starts = [y_hi, int(t + h * 0.65), int(t + h * 0.52)]
            y_end = y_lo
        else:
            y_starts = [y_lo, int(t + h * 0.35), int(t + h * 0.48)]
            y_end = y_hi
        for obs_list in (all_obstacles, strict_obstacles):
            for sy in y_starts:
                if abs(y_end - sy) < 150:
                    continue
                for sx in x_cands:
                    if not any(_in_box(sx, sy, ob) for ob in obs_list):
                        return [sx, sy, sx, y_end]
        sx = int(l + w * 0.60)
        return [sx, y_hi if dir_str == "up" else y_lo, sx, y_lo if dir_str == "up" else y_hi]

    # left / right
    y_cands = [int(t + h * f) for f in (0.50, 0.35, 0.65)]
    x_hi = r - max(20, w // 4)
    x_lo = l + max(20, w // 4)
    sx, ex = (x_hi, x_lo) if dir_str == "left" else (x_lo, x_hi)
    for obs_list in (all_obstacles, strict_obstacles):
        for sy in y_cands:
            if not any(_in_box(sx, sy, ob) for ob in obs_list):
                return [sx, sy, ex, sy]
    sy = int(t + h * 0.50)
    return [sx, sy, ex, sy]
