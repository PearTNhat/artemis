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
from typing import Any, Iterable

from artemis.workflow.fingerprint import SYSTEMUI_PACKAGE

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


def _same_kind(node: dict[str, Any], visible: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Visible entries of the same kind as ``node``: same resource id, or for
    unnamed nodes same class and width at another height (list rows)."""
    rid = node.get("resource-id") or ""
    b = _box(node)
    if rid:
        return [n for n in visible if n.get("resource-id") == rid and node_label(n)]
    return [
        n
        for n in visible
        if n.get("class") == node.get("class")
        and node_label(n)
        and abs((_box(n)[2] - _box(n)[0]) - (b[2] - b[0])) <= 10
    ]


def node_label(node: dict[str, Any]) -> str:
    return str(node.get("text") or node.get("content-desc") or "").strip()


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
            # variants): picking one is a choice for the AI / the user, even
            # when only one of them contains the value.
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


def is_list_item(node: dict[str, Any], norm: list[dict[str, Any]]) -> bool:
    """A repeated entry of scrolling content (a result, a product, a contact),
    not a control that merely floats over the list (FAB, sticky button).
    Repetition is the pattern: another visible node shares its resource id, or
    (unlabelled ids) its class and width at another height."""
    if not node.get("in_scrollable"):
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
            and abs((ob[2] - ob[0]) - width) <= 10
            and abs(ob[1] - box[1]) >= 20
            and other.get("in_scrollable")
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

    fg = foreground_package(norm)
    if not covered_by_other_window(target, norm):
        return False
    if not any(n.get("focused") and is_editable(n) and n.get("package") == fg for n in visible_app_nodes(norm)):
        return False
    for n in visible_app_nodes(norm):
        if n.get("package") in (fg, None, ""):
            continue
        b = _box(n)
        if b[2] - b[0] >= 900 and b[1] >= 400:
            return True
    return False
