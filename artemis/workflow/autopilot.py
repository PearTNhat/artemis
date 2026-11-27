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

"""Verified replay of a recorded workflow without calling the LLM (KE_HOACH Mục 4).

Each step: match the live screen to a node -> pick its single runnable edge ->
resolve the target on the live tree (unique, unoccluded, no commitment) -> act
-> verify the destination (node fingerprint AND anchors, or the typed value).
Any doubt stops the block and hands the turn to the AI with the reason.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time
from typing import Any, Protocol

from artemis.workflow.fingerprint import app_fingerprint, normalize
from artemis.workflow.matcher import DEFAULT_DELTA, DEFAULT_TAU, is_matchable, match_screen
from artemis.workflow.recorder import INPUT_TOOLS, RawScreen
from artemis.workflow.store import Edge, Workflow
from third_party.mobile_use.utils.logger import get_logger
from artemis.workflow.targets import (
    anchors_present,
    editable_field_keys,
    field_has_value,
    field_target_matches,
    fold,
    is_editable,
    keyboard_box,
    keyboard_covers,
    node_label,
    other_window_boxes,
    resolve,
    safe_swipe_in_norm,
    typed_slots,
    visible_app_nodes,
)

logger = get_logger(__name__)

MAX_STEPS = 15
# Stop scrolling primarily when a swipe leaves the screen unchanged (end of
# the list); this cap only prevents infinite scrolling (L-041).
MAX_SCROLL_SEARCH = 10
DISABLE_AFTER_CONSEC_FAIL = 2


class AutopilotDriver(Protocol):
    def current(self) -> RawScreen | None: ...

    async def refresh(self) -> RawScreen | None: ...

    async def act(self, tool: str, args: dict[str, Any], element: dict[str, Any] | None) -> tuple[bool, str]: ...


@dataclass
class AutopilotReport:
    ran: list[str] = field(default_factory=list)
    acted: int = 0  # actions sent to the device, verified or not
    revisit_node: str = ""  # live screen is one whose steps this run already did
    finished: bool = False  # replay reached the screen a successful run finished on
    stop_reason: str = ""
    stop_detail: str = ""
    elapsed_s: float = 0.0
    done_edge_ids: set[str] = field(default_factory=set)
    verified_found: list[str] = field(default_factory=list)
    verified_missing: list[str] = field(default_factory=list)
    verify_scrolls: int = 0

    @property
    def steps(self) -> int:
        return len(self.ran)

    def end_check_summary(self) -> str:
        """Concise factual summary of what the end-screen check observed (L-039)."""
        scroll_note = f" after scrolling ({self.verify_scrolls} swipe(s))" if self.verify_scrolls else ""
        if self.verified_found and not self.verified_missing:
            items = ", ".join(repr(v) for v in self.verified_found)
            return f"End-screen check CONFIRMED: verified {items} on screen{scroll_note}."
        if self.verified_missing:
            found_part = (
                f"found {', '.join(repr(v) for v in self.verified_found)}; "
                if self.verified_found
                else ""
            )
            missing_part = ", ".join(repr(v) for v in self.verified_missing)
            return (
                f"End-screen check: {found_part}did NOT see {missing_part} on screen"
                f"{scroll_note or ' (checked visible screen)'}."
            )
        return ""

    def notice(self, workflow_id: str, *, scrollable: bool = False) -> str:
        done = "; ".join(f"{i + 1}) {s}" for i, s in enumerate(self.ran)) or "none"
        if self.finished:
            check = self.end_check_summary()
            if self.verified_found and not self.verified_missing:
                return (
                    f"[Autopilot] workflow {workflow_id}: steps replayed without the model in this run:"
                    f" {done}. {check} Do NOT repeat the workflow or tap to add/create again;"
                    " call report_task_status(status='completed')."
                )
            if check:
                return (
                    f"[Autopilot] workflow {workflow_id}: steps replayed without the model in this run:"
                    f" {done}. This is the recorded end screen. {check}"
                    " Inspect the live screen before calling report_task_status. Do NOT repeat completed steps."
                )
            return (
                f"[Autopilot] workflow {workflow_id}: steps replayed without the model in this run:"
                f" {done}. Together with the steps you took yourself, the recorded path reached the recorded end screen."
                " Confirm the goal on this screen"
                + (" (it scrolls: swipe until you see the result or reach the end)" if scrollable else "")
                + " and call report_task_status. Do NOT repeat the steps above."
            )
        if self.acted > self.steps:
            done += f"; then 1 more action whose result was NOT the recorded screen"
        detail = f" ({self.stop_detail})" if self.stop_detail else ""
        return (
            f"[Autopilot] workflow {workflow_id}: replayed {self.steps} recorded step(s)"
            f" without the model: {done}. Stopped: {self.stop_reason}{detail}."
            " The screenshot and element list below are the live screen now; continue the"
            " task from here (do not redo the steps above)."
            + (
                " This screen scrolls: an item you expect (e.g. one just added) may be below"
                " the visible part, so scroll to check before concluding it is missing."
                if scrollable
                else ""
            )
        )


def _screen(raw: RawScreen | None):
    if raw is None or not raw.elements:
        return None, None
    norm = normalize(raw.elements, width=raw.width, height=raw.height)
    return norm, app_fingerprint(norm)


def _is_scrollable(norm) -> bool:
    for n in visible_app_nodes(norm or []):
        b = n.get("bounds") or (0, 0, 0, 0)
        if n.get("scrollable") and (b[2] - b[0]) * (b[3] - b[1]) >= 250_000:
            return True
    return False


def _scroll_swipe(norm) -> list[int] | None:
    """A swipe (0-1000 coords) that scrolls the app's main scrolling area and
    stays inside it, clear of any other window and floating controls. None when
    there is no safe band."""
    return safe_swipe_in_norm(norm or [], direction="up", min_container_area=250_000)


def _labels(norm) -> frozenset:
    return frozenset(
        node_label(n) or str(n.get("hint") or "").strip()
        for n in visible_app_nodes(norm or [])
        if node_label(n) or str(n.get("hint") or "").strip()
    )


def _describe(edge: Edge, element: dict[str, Any] | None, value: str) -> str:
    if edge.tool == "press_key":
        return f"press_key {edge.key}"
    if edge.tool in INPUT_TOOLS:
        return f'input_text "{value}"'
    label = node_label(element or {}) or (edge.target.resource_id if edge.target else "") or "icon"
    return f'{edge.tool} "{label}"'


REVISIT_NOTICE = (
    "[Autopilot] This screen was already passed in this run: its recorded steps"
    " ({steps}) were done before. Do not redo them to check the result.{scroll}"
)


class Autopilot:
    def __init__(
        self,
        workflow: Workflow,
        slot_values: dict[str, str],
        *,
        tau: float = DEFAULT_TAU,
        delta: float = DEFAULT_DELTA,
        max_steps: int = MAX_STEPS,
        verify_polls: int = 5,
        poll_interval_s: float = 0.6,
    ):
        self.wf = workflow
        self.slot_values = {k: v for k, v in (slot_values or {}).items() if v}
        self.tau = tau
        self.delta = delta
        self.max_steps = max_steps
        self.verify_polls = verify_polls
        self.poll_interval_s = poll_interval_s
        self.total_steps = 0
        # Forward only within a run: the furthest recorded step replayed so
        # far. A screen seen again later (the list the task started from)
        # must not restart the workflow and redo the task.
        self._done: set[str] = set()  # edge ids replayed in this run
        self._kb_hidden = False
        self._typed_ok: set[str] = set()
        self._revisit = ""
        self._finished = False
        self._assumed_node: str | None = None

    def _form_field_targets(self, node_id: str) -> list[Any]:
        return [
            e.target
            for e in self.wf.edges_from(node_id)
            if e.state == "active"
            and e.id not in self._done
            and e.tool in INPUT_TOOLS
            and e.target is not None
            and (e.target.resource_id or e.target.hint)
        ]

    def _is_multi_field_form(self, node_id: str) -> bool:
        keys = {
            (e.target.resource_id.strip(), e.target.hint.strip())
            for e in self.wf.edges_from(node_id)
            if e.state == "active"
            and e.tool in INPUT_TOOLS
            and e.target is not None
            and (e.target.resource_id or e.target.hint)
        }
        return len(keys) >= 2

    def _matches_form_fields(self, node_id: str, norm) -> bool:
        """True when a multi-field form ``node_id`` expects a field (hint/id) visible on ``norm``."""
        if not self._is_multi_field_form(node_id):
            return False
        targets = self._form_field_targets(node_id)
        if not targets:
            return False
        live_keys = editable_field_keys(norm or [])
        return field_target_matches(targets[0], live_keys)

    def _shares_form_fields(self, node_id: str, norm) -> bool:
        """True when ``norm`` displays any active input field of multi-field form ``node_id``."""
        if not self._is_multi_field_form(node_id):
            return False
        live_keys = editable_field_keys(norm or [])
        if not live_keys:
            return False
        return any(
            e.state == "active" and e.tool in INPUT_TOOLS and field_target_matches(e.target, live_keys)
            for e in self.wf.edges_from(node_id)
        )

    def _match(self, fp) -> tuple[str | None, str]:
        if fp is None:
            return None, "screen tree unavailable"
        if fp.package != self.wf.package:
            return None, f"foreground app is {fp.package or 'unknown'}, workflow is for {self.wf.package}"
        if not is_matchable(fp):
            return None, "screen tree too poor to match (WebView/canvas)"
        m = match_screen(fp, self.wf.node_fps(), tau=self.tau, delta=self.delta, min_app_tokens=1)
        if not m.matched:
            return None, f"screen not in the workflow ({m.reason}, similarity {m.score:.2f})"
        return m.node_id, ""

    def _match_live(self, norm, fp) -> tuple[str | None, str]:
        node_id, why = self._match(fp)
        assumed = self._assumed_node
        if assumed and assumed in self.wf.nodes:
            if node_id == assumed:
                return node_id, ""
            if (
                fp is not None
                and fp.package == self.wf.package
                and is_matchable(fp)
                and self._shares_form_fields(assumed, norm)
            ):
                return assumed, ""
        self._assumed_node = None
        return node_id, why

    def _field_for_target(self, target, norm) -> dict[str, Any] | None:
        field, status = resolve(target, norm)
        if field is not None:
            return field
        if status == "occluded":
            return self._occluded_node(target, norm)
        return None

    def _slot_in_own_field(self, slot: str, value: str, node_id: str, norm) -> bool:
        """True only when autopilot itself typed & verified ``slot`` in this run, or
        the recorded field for ``slot`` (by id / hint) is visible and holds ``value`` (L-036)."""
        if slot in self._typed_ok:
            return True
        targets = [
            e.target
            for e in self.wf.edges_from(node_id)
            if e.tool in INPUT_TOOLS and e.slot == slot and e.target is not None
        ]
        if not targets:
            targets = [
                e.target
                for e in self.wf.edges
                if e.tool in INPUT_TOOLS and e.slot == slot and e.target is not None
            ]
        v = fold(value)
        identified = [t for t in targets if not t.focused_editable and (t.resource_id or t.hint)]
        if not identified:
            return slot in typed_slots(norm, {slot: value})
        for t in identified:
            field = self._field_for_target(t, norm)
            if field is not None and v in fold(str(field.get("text") or "")):
                return True
        return False

    def _held_slots(self, node_id: str, norm) -> set[str]:
        return {
            slot
            for slot, val in self.slot_values.items()
            if val and self._slot_in_own_field(slot, val, node_id, norm)
        }

    def _all_slots_completed(self) -> bool:
        """True when the workflow defines slot steps and every active slot has had
        at least one step completed in this run (L-042)."""
        active_slots = {
            e.slot for e in self.wf.edges if e.state == "active" and e.kind == "slot" and e.slot
        }
        if not active_slots:
            return False
        done_slots = {
            e.slot for e in self.wf.edges if e.id in self._done and e.slot
        }
        return active_slots <= done_slots

    def _pick(self, node_id: str, norm) -> tuple[Edge | None, dict[str, Any] | None, str, str]:
        """-> (edge, element, slot value, stop reason)."""
        all_edges = self.wf.edges_from(node_id)
        edges = [e for e in all_edges if e.state == "active"]
        at_end = node_id in self.wf.end_nodes and bool(self._done)
        if at_end and not any(e.kind == "commitment_stop" for e in edges):
            self._finished = True
            return None, None, "", "reached the screen the recorded successful run finished on"
        if not edges:
            if any(e.state == "provisional" for e in all_edges):
                return (
                    None,
                    None,
                    "",
                    "this navigation step was only seen once: waiting for 2 independent runs before auto-tapping",
                )
            if (
                self._all_slots_completed()
                and not any(e.kind == "commitment_stop" for e in all_edges)
                and not self._form_still_open(norm)
            ):
                self._finished = True
                return None, None, "", "all recorded slot steps are completed on a workflow screen"
            return None, None, "", "no recorded step from this screen"
        if all(e.id in self._done for e in edges):
            self._revisit = node_id
            if (
                self._all_slots_completed()
                and not any(e.kind == "commitment_stop" for e in edges)
                and not self._form_still_open(norm)
            ):
                self._finished = True
            return None, None, "", "the recorded steps from this screen were already replayed in this run"
        # Never replay a step twice in one run (merged workflows are graphs:
        # the file order is not the path order, so only what ran counts).
        edges = [e for e in edges if e.id not in self._done]
        if any(e.kind == "commitment_stop" for e in edges):
            return None, None, "", "a commitment action (order/payment/confirm) comes next: the AI must check and decide"
        if any(e.kind == "judgement" for e in edges):
            return None, None, "", "this screen needs a choice or judgement (recorded as the AI's / user's decision)"
        cands: dict[tuple, tuple[Edge, dict[str, Any] | None, str]] = {}
        reasons: list[str] = []
        # Typed values: verified in this run by autopilot, or visible in the
        # exact field recorded for that slot (never in an unrelated field, L-036).
        held = self._held_slots(node_id, norm)
        for e in edges:
            value = self.slot_values.get(e.slot, "") if e.slot else ""
            if e.slot and not value:
                reasons.append(f"value for {e.slot} unknown for this task")
                continue
            need = {t for t in (e.typed or "").split(",") if t}
            missing = need - held
            if missing:
                reasons.append(f"waits for {', '.join(sorted(missing))} to be typed")
                continue
            if e.tool in INPUT_TOOLS and e.slot in held:
                continue  # already typed: the next recorded step applies
            if e.target is None:
                cands[(e.tool, e.key)] = (e, None, value)
                continue
            el, status = resolve(e.target, norm, slot_value=value if e.target.slot else None)
            if el is not None and e.tool in INPUT_TOOLS:
                # Never type over a value this run already typed for another
                # field (a wrong recording would overwrite it, L-029).
                held_text = fold(str(el.get("text") or ""))
                clash = [
                    k for k in self._typed_ok
                    if k != e.slot and self.slot_values.get(k) and fold(self.slot_values[k]) in held_text
                ]
                if clash:
                    reasons.append(f"field already holds {', '.join(sorted(clash))} typed in this run")
                    continue
                if e.empty_on_record and field_has_value(el, e.target.hint if e.target else ""):
                    return (
                        None,
                        None,
                        "",
                        "field already holds existing data (recorded when empty): handing over to AI",
                    )
            if status == "occluded" and not self._kb_hidden:
                hidden_el = self._occluded_node(e.target, norm)
                if hidden_el is not None and keyboard_covers(hidden_el, norm):
                    return None, None, "", "keyboard covers the target"
            if status != "ok" or el is None:
                reasons.append(f"target {status}")
                continue
            cands[(e.tool, e.key, tuple(el["bounds"]))] = (e, el, value)
        if len(cands) == 1:
            e, el, value = next(iter(cands.values()))
            return e, el, value, ""
        if len(cands) > 1 and all(e.tool in INPUT_TOOLS for e, _, _ in cands.values()):
            # Several fields still to fill on one form: each typing targets its
            # own field and is verified there, so take them in recorded order.
            first = min(cands.values(), key=lambda c: self.wf.edges.index(c[0]))
            return first[0], first[1], first[2], ""
        if len(cands) > 1:
            return None, None, "", "several recorded steps apply here"
        waiting = [r for r in reasons if r.startswith("waits for ")]
        blocking = [r for r in reasons if not r.startswith("waits for ")]
        if blocking and all(r == "target not_found" for r in blocking):
            return None, None, "", "recorded target not found"
        if not reasons:
            return None, None, "", "recorded target not found"
        return None, None, "", (blocking[0] if blocking else waiting[0])

    @staticmethod
    def _occluded_node(desc, norm):
        """The node ``desc`` names, ignoring occlusion (to see what covers it)."""
        from artemis.workflow.targets import TargetDescriptor, visible_app_nodes as _vis

        for n in _vis(norm):
            if desc.resource_id and n.get("resource-id") == desc.resource_id:
                return n
            if desc.hint and n.get("hint") == desc.hint:
                return n
        return None

    def _verified(self, edge: Edge, norm, fp, value: str) -> bool:
        if norm is None or fp is None:
            return False
        if edge.tool in INPUT_TOOLS:
            v = fold(value)
            if edge.target is not None and not edge.target.focused_editable:
                # The value must sit in the recorded field, not just anywhere.
                field = self._field_for_target(edge.target, norm)
                if field is not None:
                    return v in fold(str(field.get("text") or ""))
                if edge.target.resource_id or edge.target.hint:
                    return False
            return any(
                v in fold(str(n.get("text") or ""))
                for n in visible_app_nodes(norm)
                if n.get("editable") or "EditText" in str(n.get("class") or "")
            )
        if fp.package != self.wf.package or not is_matchable(fp):
            return False
        # The expected node only has to be the best match above tau: a sparse
        # destination may sit close to a sibling node (no margin needed when we
        # know which screen should come), and the anchors confirm it.
        m = match_screen(fp, self.wf.node_fps(), tau=self.tau, delta=0.0, min_app_tokens=1)
        anchors = anchors_present(edge.anchors, norm)
        form_ok = bool(anchors and edge.dst and self._matches_form_fields(edge.dst, norm))
        logger.info(
            f"Autopilot verify {edge.id}: expect {edge.dst}, best {m.node_id} ({m.score:.2f}),"
            f" anchors={'ok' if anchors else 'missing'}, form={'ok' if form_ok else 'no'},"
            f" app_tokens={fp.app_token_count}"
        )
        return ((m.matched and m.node_id == edge.dst) or form_ok) and anchors

    def _expected_end_values(self) -> list[str]:
        """Task slot values expected to appear on the workflow's end screen (L-039)."""
        used_slots = {e.slot for e in self.wf.edges if e.kind == "slot" and e.slot}
        out: list[str] = []
        seen: set[str] = set()
        for slot, val in self.slot_values.items():
            if not val or (used_slots and slot not in used_slots):
                continue
            key = fold(val)
            if key and key not in seen:
                seen.add(key)
                out.append(val)
        return out

    def _form_still_open(self, norm) -> bool:
        """True when an editable field on screen still holds a typed task value (L-044)."""
        return bool(typed_slots(norm or [], self.slot_values))

    @staticmethod
    def _values_on_screen(norm, values: list[str]) -> set[str]:
        labels = [
            fold(node_label(n))
            for n in visible_app_nodes(norm or [])
            if not is_editable(n) and node_label(n)
        ]
        return {v for v in values if fold(v) and any(fold(v) in lb for lb in labels)}

    async def _verify_end_screen(self, driver: AutopilotDriver, norm, rep: AutopilotReport) -> None:
        """Checks whether expected slot values appear on the end screen, scrolling
        safely until found, list end, or ``MAX_SCROLL_SEARCH`` (L-039, L-041)."""
        expected = self._expected_end_values()
        if not expected:
            return
        found = self._values_on_screen(norm, expected)
        cur_norm = norm
        vscrolls = 0
        while len(found) < len(expected) and _is_scrollable(cur_norm) and vscrolls < MAX_SCROLL_SEARCH:
            swipe = _scroll_swipe(cur_norm)
            if swipe is None:
                break
            before = _labels(cur_norm)
            ok, _ = await driver.act(
                "swipe",
                {
                    "coordinates": swipe,
                    "target_description": "autopilot: scroll to verify recorded result on end screen",
                },
                None,
            )
            if not ok:
                break
            rep.acted += 1
            vscrolls += 1
            snorm, _ = _screen(driver.current())
            if snorm is None:
                break
            found |= self._values_on_screen(snorm, expected)
            if _labels(snorm) == before:
                break
            cur_norm = snorm
        rep.verified_found = [v for v in expected if v in found]
        rep.verified_missing = [v for v in expected if v not in found]
        rep.verify_scrolls = vscrolls

    def _note(self, edge: Edge, ok: bool) -> None:
        if ok:
            edge.ok += 1
            edge.consec_fail = 0
        else:
            edge.fail += 1
            edge.consec_fail += 1
            if edge.consec_fail >= DISABLE_AFTER_CONSEC_FAIL:
                edge.state = "disabled"

    async def run(self, driver: AutopilotDriver) -> AutopilotReport:
        rep = AutopilotReport()
        started = time.monotonic()
        self._revisit = ""
        scrolls = 0
        try:
            while True:
                if self.total_steps >= self.max_steps:
                    rep.stop_reason = f"replay limit of {self.max_steps} steps reached"
                    break
                norm, fp = _screen(driver.current())
                node_id, why = self._match_live(norm, fp)
                if node_id is None:
                    rep.stop_reason = why
                    break
                edge, element, value, why = self._pick(node_id, norm)
                if edge is None and why == "keyboard covers the target":
                    self._kb_hidden = True
                    ok, _ = await driver.act("press_key", {"key": "BACK"}, None)
                    rep.acted += 1
                    if not ok:
                        rep.stop_reason = "could not hide the keyboard"
                        break
                    rep.ran.append("press_key BACK (hide the keyboard over the target) -> ok")
                    continue
                if edge is None and why == "recorded target not found" and _is_scrollable(norm):
                    if scrolls >= MAX_SCROLL_SEARCH:
                        rep.stop_reason = f"recorded target not found after {scrolls} swipes"
                        break
                    if _scroll_swipe(norm) is None and keyboard_box(norm) is not None and not self._kb_hidden:
                        # No band to swipe clear of the keyboard: hide it first.
                        self._kb_hidden = True
                        ok, _ = await driver.act("press_key", {"key": "BACK"}, None)
                        rep.acted += 1
                        if not ok:
                            rep.stop_reason = "could not hide the keyboard before scrolling"
                            break
                        rep.ran.append("press_key BACK (hide the keyboard before scrolling) -> ok")
                        continue
                    swipe = _scroll_swipe(norm)
                    if swipe is None:
                        rep.stop_reason = "recorded target not found and no safe area to scroll"
                        break
                    before = _labels(norm)
                    ok, _ = await driver.act(
                        "swipe",
                        {
                            "coordinates": swipe,
                            "target_description": "autopilot: scroll to find the recorded field",
                        },
                        None,
                    )
                    rep.acted += 1
                    scrolls += 1
                    snorm, sfp = _screen(driver.current())
                    if not ok or _labels(snorm) == before:
                        rep.stop_reason = "recorded target not found (end of the list reached)"
                        break
                    if self._match(sfp)[0] != node_id and not self._shares_form_fields(node_id, snorm):
                        rep.stop_reason = "scrolling for the recorded target left the recorded screen"
                        break
                    rep.ran.append("swipe up (looking for the recorded target) -> ok")
                    continue
                if edge is None:
                    if self._finished:
                        await self._verify_end_screen(driver, norm, rep)
                    rep.stop_reason = why
                    break
                args: dict[str, Any] = {}
                if edge.tool == "press_key":
                    args = {"key": edge.key}
                elif edge.tool in INPUT_TOOLS:
                    args = {"text": value}
                    if element is not None:
                        args["target"] = 1
                else:
                    args = {"target": 1}
                desc = _describe(edge, element, value)
                ok, summary = await driver.act(edge.tool, args, element)
                rep.acted += 1
                self._done.add(edge.id)
                self._kb_hidden = False
                if not ok:
                    self._note(edge, False)
                    rep.stop_reason = f"step '{desc}' failed"
                    rep.stop_detail = summary[:160]
                    break
                verified = False
                vnorm = None
                for attempt in range(self.verify_polls):
                    raw = driver.current() if attempt == 0 else await driver.refresh()
                    vnorm, vfp = _screen(raw)
                    if self._verified(edge, vnorm, vfp, value):
                        verified = True
                        break
                    await asyncio.sleep(self.poll_interval_s)
                if verified and edge.dst and self._shares_form_fields(edge.dst, vnorm):
                    self._assumed_node = edge.dst
                else:
                    self._assumed_node = None
                self._note(edge, verified)
                report = getattr(driver, "note_verified", None)
                if callable(report):
                    report(verified)  # UI: verified workflow step vs taken over by the AI
                if verified and edge.tool in INPUT_TOOLS and edge.slot:
                    self._typed_ok.add(edge.slot)
                self.total_steps += 1
                if not verified:
                    rep.stop_reason = f"after '{desc}' the screen is not the recorded next screen"
                    break
                rep.ran.append(desc + " -> ok")
                scrolls = 0
        except Exception as exc:  # never break the run over the accelerator
            logger.warning(f"Autopilot stopped on error: {exc}")
            rep.stop_reason = f"autopilot error: {exc}"
        rep.finished = self._finished
        rep.revisit_node = self._revisit if rep.acted == 0 else ""
        rep.done_edge_ids = set(self._done)
        rep.elapsed_s = round(time.monotonic() - started, 3)
        return rep
