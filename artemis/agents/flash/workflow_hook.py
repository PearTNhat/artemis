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

"""FlashRunner <-> app-workflow glue (KE_HOACH_APP_WORKFLOW Mục 6).

- record: every device action is kept in memory with the raw UI tree before
  and after it (``ctx.last_raw_observation``, published by ``observe``); a run
  the AI reports ``completed`` becomes / extends a workflow on disk.
- replay: at the top of each turn, before the model is called, the autopilot
  replays the recorded steps it can verify, then hands over with a notice and a
  forced screenshot. No synthetic AI/Tool messages are created (AGENTS.md 2.6).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
import uuid

from langchain_core.messages import HumanMessage

from artemis.workflow.autopilot import REVISIT_NOTICE, Autopilot
from artemis.workflow.fingerprint import app_fingerprint, normalize
from artemis.workflow.recorder import RawScreen, RecordedStep, record_run
from artemis.workflow.selector import select_workflow
from artemis.workflow.store import Workflow, WorkflowStore
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

MODES = ("off", "record", "replay")


def resolve_mode(cfg_workflow: Any) -> tuple[str, str | None, int]:
    env = os.environ.get("ARTEMIS_WORKFLOW_MODE", "").strip().lower()
    mode = getattr(cfg_workflow, "mode", None) if cfg_workflow is not None else None
    if isinstance(cfg_workflow, dict):
        mode = cfg_workflow.get("mode")
    mode = env or str(mode or "off").lower()
    if mode not in MODES:
        mode = "off"
    d = os.environ.get("ARTEMIS_WORKFLOW_DIR") or (
        cfg_workflow.get("dir") if isinstance(cfg_workflow, dict) else getattr(cfg_workflow, "dir", None)
    )
    max_steps = (
        cfg_workflow.get("max_steps") if isinstance(cfg_workflow, dict) else getattr(cfg_workflow, "max_steps", None)
    )
    return mode, (str(d) if d else None), int(max_steps or 15)


class _Driver:
    def __init__(self, hook: "FlashWorkflowHook", state: Any):
        self.hook = hook
        self.state = state

    def current(self) -> RawScreen | None:
        return self.hook.snapshot()

    async def refresh(self) -> RawScreen | None:
        """Re-captures through observe() (same path and format as recording);
        observe publishes the raw tree on ctx, which current() reads."""
        runner = self.hook.runner
        try:
            session = await runner.executor._session_or_start()
            await session.observe(settle_ms=0, persist=False)
        except Exception as exc:
            logger.warning(f"Autopilot refresh failed: {exc}")
            return None
        return self.hook.snapshot()

    async def act(self, tool: str, args: dict[str, Any], element: dict[str, Any] | None) -> tuple[bool, str]:
        from artemis.agents.flash.runner import _TurnRecord

        hook = self.hook
        runner = hook.runner
        index_elements = None
        if element is not None:
            cur = hook.snapshot()
            w = (cur.width if cur else None) or 1080
            h = (cur.height if cur else None) or 2400
            l, t, r, b = element["bounds"]
            px = [round(l * w / 1000), round(t * h / 1000), round(r * w / 1000), round(b * h / 1000)]
            index_elements = [
                {
                    "center": [(px[0] + px[2]) // 2, (px[1] + px[3]) // 2],
                    "bounds": px,
                    "text": element.get("text") or element.get("content-desc") or "",
                    "resource_id": element.get("resource-id") or "",
                    "class": element.get("class") or "",
                }
            ]
        turn = _TurnRecord()
        hook._in_autopilot = True
        try:
            hook._bytes, hook._xml, hook._seq = await runner._execute_and_record_action(
                tool,
                args,
                f"autopilot_{uuid.uuid4().hex[:8]}",
                self.state,
                [],
                f"[autopilot] workflow {hook.workflow.workflow_id if hook.workflow else ''}",
                {},
                hook._bytes,
                hook._xml,
                hook._seq,
                turn,
                index_elements=index_elements,
            )
        finally:
            hook._in_autopilot = False
        if not turn.actions:
            return False, "not executed"
        _, status, summary = turn.actions[-1]
        if status != "success" or turn.had_no_effect or turn.had_ineffective_tap:
            return False, str(summary or status)
        return True, str(summary or "")


class FlashWorkflowHook:
    def __init__(self, runner: Any, mode: str, store_dir: str | None = None, max_steps: int = 15):
        self.runner = runner
        self.mode = mode
        self.store = WorkflowStore(store_dir)
        self.max_steps = max_steps
        self.steps: list[RecordedStep] = []
        self.workflow: Workflow | None = None
        self.slot_values: dict[str, str] = {}
        self._selected_for: str | None = None
        self._autopilot: Autopilot | None = None
        self._choice_pending = False
        self._in_autopilot = False
        self._last_pre_seq: int | None = None
        self._bytes = None
        self._xml = None
        self._seq = 0
        self.autopilot_steps = 0
        self._revisit_warned: set[str] = set()
        self._done_steps: list[str] = []
        self.selector_calls = 0

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    # --- recording -------------------------------------------------------------

    def snapshot(self) -> RawScreen | None:
        obs = getattr(self.runner.ctx, "last_raw_observation", None)
        if not isinstance(obs, dict) or not obs.get("elements"):
            return None
        return RawScreen(obs.get("elements"), obs.get("width"), obs.get("height"), obs.get("seq"))

    def note_user_choice(self) -> None:
        self._choice_pending = True

    def after_action(self, name: str, args: dict, action: dict | None, exec_result: Any, pre: RawScreen | None) -> None:
        if not self.enabled:
            return
        post = self.snapshot()
        pre_seq = getattr(pre, "seq", None)
        if pre is not None and pre_seq is not None and pre_seq == self._last_pre_seq:
            pre = None  # burst action: the screen it acted on was never observed
        self._last_pre_seq = pre_seq
        if post is not None and getattr(post, "seq", None) == pre_seq:
            post = None
        meta = getattr(exec_result, "metadata", None) or {}
        summary = str(getattr(exec_result, "text_summary", "") or "")
        no_effect = bool(meta.get("no_effect")) or "(no visible change)" in summary
        after_choice = self._choice_pending and name in ("click", "tap", "long_press", "input_text")
        if after_choice:
            self._choice_pending = False
        logger.info(
            f"Workflow rec: {name} ok={getattr(exec_result, 'status', '')} no_effect={no_effect}"
            f" pre_seq={pre_seq if pre is not None else None}"
            f" post_seq={getattr(post, 'seq', None)} autopilot={self._in_autopilot}"
        )
        self.steps.append(
            RecordedStep(
                tool=name,
                args=dict(args or {}),
                action=dict(action or {}),
                ok=getattr(exec_result, "status", "") == "success",
                no_effect=no_effect,
                pre=pre,
                post=post,
                after_user_choice=after_choice,
                autopilot=self._in_autopilot,
            )
        )

    async def finish(self, final_report: dict | None) -> None:
        if not self.enabled:
            return
        completed = str((final_report or {}).get("status", "")).lower() == "completed"
        try:
            wf = self.workflow
            if wf is not None:
                wf.runs += 1
                wf.ok_runs += int(completed)
            if completed and wf is None:
                # No workflow was selected during the run (always the case in
                # "record" mode, which never runs the selector): look for one
                # of the same kind now, so a repeated task is merged into it
                # instead of creating a new file each time.
                wf = await self._select_for_merge()
                if wf is not None:
                    wf.runs += 1
                    wf.ok_runs += 1
            if completed:
                built = record_run(
                    self.steps,
                    goal=self.runner.goal,
                    workflow=wf,
                    slot_values=self.slot_values if wf is not None else None,
                )
                if built is not None and wf is None:
                    built.workflow_id = self.store.new_id(built.package)
                    built.runs = built.ok_runs = 1
                wf = built or wf
            if wf is not None:
                path = self.store.save(wf)
                logger.info(
                    f"Workflow {wf.workflow_id} saved ({len(wf.nodes)} screens, {len(wf.edges)} steps,"
                    f" autopilot_steps_this_run={self.autopilot_steps}) -> {path}"
                )
        except Exception as exc:
            logger.warning(f"Workflow recording failed: {exc}")

    async def _select_for_merge(self) -> Workflow | None:
        """After a successful run with no selected workflow: the stored workflow
        of the same kind in the app the run worked in, with this run's slot
        values (None when there is none)."""
        probe = record_run(self.steps, goal=self.runner.goal)
        if probe is None:
            return None
        cands = [w for w in self.store.list(probe.package) if w.edges]
        if not cands:
            return None
        wf, values = await select_workflow(self.runner.goal, cands, self._ask_text)
        if wf is not None:
            self.slot_values = values
            logger.info(f"Workflow merge target: {wf.workflow_id} slots={values}")
        return wf

    # --- replay ----------------------------------------------------------------

    async def _ask_text(self, prompt: str) -> str:
        llm = getattr(self.runner, "_llm", None)
        if llm is None:
            return ""
        self.selector_calls += 1
        resp = await asyncio.wait_for(llm.ainvoke([HumanMessage(content=prompt)]), timeout=90)
        return self.runner._extract_response_text(resp)

    async def _select(self, package: str) -> None:
        self._selected_for = package
        cands = self.store.list(package)
        cands = [w for w in cands if any(e.state == "active" and e.kind in ("nav", "slot") for e in w.edges)]
        wf, values = await select_workflow(self.runner.goal, cands, self._ask_text if cands else None)
        self.workflow, self.slot_values = wf, values
        self._autopilot = (
            Autopilot(wf, values, max_steps=self.max_steps) if wf is not None else None
        )
        if wf is not None:
            logger.info(f"Workflow selected: {wf.workflow_id} slots={values}")

    async def maybe_autopilot(self, state: Any, img_bytes: Any, xml_list: Any):
        """Runs the autopilot when the live screen is a recorded one.

        Returns ``(img_bytes, xml_list, notice)`` after at least one replayed
        step, else None (the turn proceeds unchanged).
        """
        if self.mode != "replay":
            return None
        cur = self.snapshot()
        if cur is None:
            return None
        package = app_fingerprint(normalize(cur.elements, width=cur.width, height=cur.height)).package
        if not package:
            return None
        if self.workflow is None and package != self._selected_for:
            await self._select(package)
        if self._autopilot is None or self.workflow is None or package != self.workflow.package:
            return None
        self._bytes, self._xml = img_bytes, xml_list
        rep = await self._autopilot.run(_Driver(self, state))
        logger.info(
            f"Autopilot: {rep.steps} step(s) in {rep.elapsed_s}s; stopped: {rep.stop_reason}"
        )
        if rep.acted == 0 and rep.finished and "finished" not in self._revisit_warned:
            # Back on the end screen without acting (e.g. the AI did the last
            # pick itself): say once that the recorded run finished here.
            self._revisit_warned.add("finished")
            rep.ran = [f"(earlier in this run) {d}" for d in self._done_steps] or rep.ran
            return img_bytes, xml_list, rep.notice(
                self.workflow.workflow_id, scrollable=bool(getattr(state, "screen_scrollable", False))
            )
        if rep.acted == 0:
            # Nothing touched the device: the turn's observation stands. Back on
            # a screen this run already got past (the list the task started
            # from): warn once, before the model redoes the steps to "check".
            if rep.revisit_node and rep.revisit_node not in self._revisit_warned:
                self._revisit_warned.add(rep.revisit_node)
                steps = ", ".join(
                    f"{e.tool} {e.key or (e.target.text if e.target else '') or ''}".strip()
                    for e in self.workflow.edges_from(rep.revisit_node)
                    if e.kind in ("nav", "slot")
                )
                scroll = (
                    " This screen scrolls: what was added may be below the visible part,"
                    " so scroll to check it."
                    if getattr(state, "screen_scrollable", False)
                    else ""
                )
                return img_bytes, xml_list, REVISIT_NOTICE.format(steps=steps or "recorded", scroll=scroll)
            return None
        self.autopilot_steps += rep.steps
        self._done_steps.extend(rep.ran)
        if rep.finished:
            self._revisit_warned.add("finished")
        new_bytes, new_xml = await self.runner._reobserve_after_resume(state)
        self.runner._force_image = True
        scrollable = bool(getattr(state, "screen_scrollable", False))
        return new_bytes, new_xml, rep.notice(self.workflow.workflow_id, scrollable=scrollable)
