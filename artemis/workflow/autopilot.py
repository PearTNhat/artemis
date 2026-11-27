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
from artemis.workflow.targets import anchors_present, fold, keyboard_covers, node_label, resolve, typed_slots, visible_app_nodes

logger = get_logger(__name__)

MAX_STEPS = 15
# Target recorded below the visible part (the AI scrolled before tapping): look
# for it with at most this many swipes per screen; an unchanged screen after a
# swipe is the end of the list.
MAX_SCROLL_SEARCH = 3
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

    @property
    def steps(self) -> int:
        return len(self.ran)

    def notice(self, workflow_id: str, *, scrollable: bool = False) -> str:
        done = "; ".join(f"{i + 1}) {s}" for i, s in enumerate(self.ran)) or "none"
        if self.finished:
            return (
                f"[Autopilot] workflow {workflow_id}: steps replayed without the model in this run:"
                f" {done}. Together with the steps you took yourself, the recorded path is complete, and"
                " this is the screen the earlier successful run was reported completed on: the task is"
                " most likely done."
                " Confirm the goal on this screen"
                + (" (it scrolls: swipe to find a just-added item before deciding)" if scrollable else "")
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


def _labels(norm) -> frozenset:
    return frozenset(node_label(n) for n in visible_app_nodes(norm or []) if node_label(n))


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
        self._revisit = ""
        self._finished = False

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

    def _pick(self, node_id: str, norm) -> tuple[Edge | None, dict[str, Any] | None, str, str]:
        """-> (edge, element, slot value, stop reason)."""
        edges = [e for e in self.wf.edges_from(node_id) if e.state == "active"]
        at_end = node_id in self.wf.end_nodes and bool(self._done)
        if at_end and not any(e.kind == "commitment_stop" for e in edges):
            self._finished = True
            return None, None, "", "reached the screen the recorded successful run finished on"
        if not edges:
            return None, None, "", "no recorded step from this screen"
        if all(e.id in self._done for e in edges):
            self._revisit = node_id
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
        held = typed_slots(norm, self.slot_values)
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
        if not reasons or all(r == "target not_found" for r in reasons):
            return None, None, "", "recorded target not found"
        return None, None, "", reasons[0]

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
                field, _ = resolve(edge.target, norm)
                if field is not None:
                    return v in fold(str(field.get("text") or ""))
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
        logger.info(
            f"Autopilot verify {edge.id}: expect {edge.dst}, best {m.node_id} ({m.score:.2f}),"
            f" anchors={'ok' if anchors else 'missing'}, app_tokens={fp.app_token_count}"
        )
        return m.matched and m.node_id == edge.dst and anchors

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
                node_id, why = self._match(fp)
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
                    before = _labels(norm)
                    ok, _ = await driver.act("swipe", {"direction": "up"}, None)
                    rep.acted += 1
                    scrolls += 1
                    snorm, sfp = _screen(driver.current())
                    if not ok or _labels(snorm) == before:
                        rep.stop_reason = "recorded target not found (end of the list reached)"
                        break
                    if self._match(sfp)[0] != node_id:
                        rep.stop_reason = "scrolling for the recorded target left the recorded screen"
                        break
                    rep.ran.append("swipe up (looking for the recorded target) -> ok")
                    continue
                if edge is None:
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
                for attempt in range(self.verify_polls):
                    raw = driver.current() if attempt == 0 else await driver.refresh()
                    vnorm, vfp = _screen(raw)
                    if self._verified(edge, vnorm, vfp, value):
                        verified = True
                        break
                    await asyncio.sleep(self.poll_interval_s)
                self._note(edge, verified)
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
        rep.elapsed_s = round(time.monotonic() - started, 3)
        return rep
