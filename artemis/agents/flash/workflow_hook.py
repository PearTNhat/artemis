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
import re
from typing import Any
import uuid

from langchain_core.messages import HumanMessage

from artemis.workflow.autopilot import REVISIT_NOTICE, Autopilot
from artemis.workflow.fingerprint import ScreenFingerprint, app_fingerprint, normalize
from artemis.workflow.playbook import build_playbook, next_playbook_step
from artemis.workflow.recorder import RawScreen, RecordedStep, record_run
from artemis.workflow.selector import filter_by_live_screen, select_workflow, select_workflow_fast
from artemis.workflow.store import Workflow, WorkflowStore
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

MODES = ("off", "record", "replay")


_LAUNCHER_RESOLVE_CMD = "cmd package resolve-activity -a android.intent.action.MAIN -c android.intent.category.HOME"


def _parse_launcher_package(output: str) -> str | None:
    m = re.search(r"packageName=([^\s]+)", output or "")
    if m:
        pkg = m.group(1).strip()
        if pkg and "." in pkg and pkg != "android":
            return pkg
    m2 = re.search(r"([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/", output or "")
    if m2:
        pkg = m2.group(1).strip()
        if pkg != "android":
            return pkg
    return None


def _app_names_from_ctx(ctx: Any) -> list[str]:
    names: list[str] = []
    pkg_cache = getattr(ctx, "package_cache", None)
    if isinstance(pkg_cache, dict):
        for k, v in pkg_cache.items():
            if v and str(k).strip() and str(k).strip() != str(v).strip():
                names.append(str(k).strip())
    return names


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
        # The DataEngine step this action was recorded as (for workflow_verified).
        keys = getattr(turn, "step_keys", None) or []
        self.last_step_id = keys[-1] if keys else None
        if not turn.actions:
            return False, "not executed"
        _, status, summary = turn.actions[-1]
        if status != "success" or turn.had_no_effect or turn.had_ineffective_tap:
            self.note_verified(False)
            return False, str(summary or status)
        return True, str(summary or "")

    def note_verified(self, verified: bool) -> None:
        """Stamps the destination check result on the step just recorded, so the
        UI tells verified workflow steps from ones the AI had to take over."""
        engine = getattr(self.hook.runner.ctx, "data_engine", None)
        step_id = getattr(self, "last_step_id", None)
        if engine is None or not step_id or not hasattr(engine, "merge_step_extra_metadata"):
            return
        try:
            engine.merge_step_extra_metadata(step_id, {"workflow_verified": bool(verified)})
        except Exception as exc:
            logger.debug(f"Could not stamp workflow_verified on step {step_id}: {exc}")


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
        self._playbook_sent = False
        self._tried_cross_app = False
        self._end_guard_armed = False
        self.selector_calls = 0
        self._publish_session_workflow()

    def _arm_end_guard(self, rep: Any) -> None:
        """Arms executor one-shot guard against restarting after reaching end screen (L-039)."""
        if self._end_guard_armed or self.workflow is None:
            return
        executor = getattr(self.runner, "executor", None)
        arm = getattr(executor, "arm_workflow_end_guard", None)
        if not callable(arm):
            return
        from artemis.workflow.targets import resolve

        snap = self.snapshot()
        norm = normalize(snap.elements, width=snap.width, height=snap.height) if snap else None
        node_ids = list(dict.fromkeys(list(self.workflow.start_nodes) + list(self.workflow.end_nodes)))
        targets: list[dict[str, Any]] = []
        seen_sigs: set[tuple] = set()
        for nid in node_ids:
            for e in self.workflow.edges_from(nid):
                if e.state != "active" or e.kind != "nav":
                    continue
                sig = e.signature()
                if sig in seen_sigs:
                    continue
                seen_sigs.add(sig)
                entry: dict[str, Any] = {"tool": e.tool, "key": e.key}
                if e.target is not None:
                    entry.update({
                        "resource_id": e.target.resource_id,
                        "text": e.target.text,
                        "class_name": e.target.class_name,
                        "grid": list(e.target.grid),
                    })
                    if norm is not None:
                        el, _ = resolve(e.target, norm)
                        if el is not None:
                            entry["bounds"] = list(el["bounds"])
                targets.append(entry)
        if targets:
            self._end_guard_armed = True
            arm(targets, rep.end_check_summary() or "Workflow already reached its recorded end screen.")

    def _publish_session_workflow(self, *, saved: bool = False) -> None:
        """Persists workflow metadata onto the session's ``device_info`` for UI/history badges."""
        if not self.enabled:
            return
        de = getattr(getattr(self.runner, "ctx", None), "data_engine", None)
        upd = getattr(de, "update_session_device_info", None)
        if not callable(upd):
            return
        info: dict[str, Any] = {
            "mode": self.mode,
            "autopilot_steps": self.autopilot_steps,
            "selector_calls": self.selector_calls,
        }
        if self.workflow is not None:
            info["workflow_id"] = self.workflow.workflow_id
            info["package"] = self.workflow.package
        if self.slot_values:
            info["slots"] = dict(self.slot_values)
        if saved:
            info["saved"] = True
        upd(workflow=info)

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
            if not completed:
                if wf is not None:
                    wf.runs += 1
                    path = self.store.save(wf)
                    self.workflow = wf
                    self._publish_session_workflow(saved=False)
                return
            if wf is None:
                # No workflow was selected during the run (always the case in
                # "record" mode, which never runs the selector): look for one
                # of the same kind now, so a repeated task is merged into it
                # instead of creating a new file each time.
                wf = await self._select_for_merge()
            app_names = _app_names_from_ctx(getattr(self.runner, "ctx", None))
            built = record_run(
                self.steps,
                goal=self.runner.goal,
                workflow=wf,
                slot_values=self.slot_values if wf is not None else None,
                app_names=app_names,
            )
            if built is None:
                return
            if wf is None:
                built.workflow_id = self.store.new_id(built.package)
                built.runs = built.ok_runs = 1
            else:
                built.runs += 1
                built.ok_runs += 1
            wf = built
            path = self.store.save(wf)
            self.workflow = wf
            self._publish_session_workflow(saved=True)
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
        probe = record_run(
            self.steps,
            goal=self.runner.goal,
            app_names=_app_names_from_ctx(getattr(self.runner, "ctx", None)),
        )
        if probe is None:
            return None
        idx_cands = [e for e in self.store.list_index(probe.package) if e.step_count > 0]
        if not idx_cands:
            return None
        entry, values = select_workflow_fast(self.runner.goal, idx_cands)
        if entry is not None:
            wf = self.store.load(probe.package, entry.workflow_id)
        else:
            cands = [w for w in self.store.list(probe.package) if w.edges]
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

    async def _select_from_package(
        self, package: str, live_fp: ScreenFingerprint | None = None
    ) -> tuple[Workflow | None, dict[str, str]]:
        idx_cands = [e for e in self.store.list_index(package) if e.has_runnable]
        if not idx_cands:
            return None, {}
        entry, values = select_workflow_fast(self.runner.goal, idx_cands)
        if entry is not None:
            loaded = self.store.load(package, entry.workflow_id)
            if loaded is not None:
                return loaded, values
        cands = [w for w in self.store.list(package) if any(e.state == "active" and e.kind in ("nav", "slot") for e in w.edges)]
        if not cands:
            return None, {}
        cands = filter_by_live_screen(cands, live_fp)
        return await select_workflow(self.runner.goal, cands, self._ask_text, live_fp=live_fp)

    async def _select(self, package: str, live_fp: ScreenFingerprint | None = None) -> None:
        self._selected_for = package
        wf, values = await self._select_from_package(package, live_fp=live_fp)
        self.workflow, self.slot_values = wf, values
        self._autopilot = (
            Autopilot(wf, values, max_steps=self.max_steps) if wf is not None else None
        )
        if wf is not None:
            logger.info(f"Workflow selected: {wf.workflow_id} slots={values}")

    async def _launcher_package(self) -> str | None:
        """Resolves Android's default HOME launcher package once per device (L-024)."""
        ctx = getattr(self.runner, "ctx", None)
        if ctx is None:
            return None
        explicit = getattr(ctx, "launcher_package", None)
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        cache = getattr(ctx, "_workflow_launcher_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            try:
                setattr(ctx, "_workflow_launcher_cache", cache)
            except Exception:
                pass
        dev_id = str(getattr(getattr(ctx, "device", None), "device_id", None) or "default")
        if dev_id in cache:
            return cache[dev_id]
        pkg: str | None = None
        try:
            ctrl = getattr(getattr(getattr(self.runner, "executor", None), "actuator", None), "controller", None)
            drv = getattr(ctrl, "_driver", None)
            if drv is not None and callable(getattr(drv, "execute_shell", None)):
                raw = await drv.execute_shell(_LAUNCHER_RESOLVE_CMD)
                pkg = _parse_launcher_package(str(raw or ""))
            else:
                from third_party.mobile_use.controllers.platform_specific_commands_controller import get_adb_device

                adb_dev = get_adb_device(ctx)
                if adb_dev is not None:
                    raw = await asyncio.to_thread(adb_dev.shell, _LAUNCHER_RESOLVE_CMD)
                    pkg = _parse_launcher_package(str(raw or ""))
        except Exception as exc:
            logger.debug(f"Resolve default launcher failed: {exc}")
        cache[dev_id] = pkg
        return pkg

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
        fp = app_fingerprint(normalize(cur.elements, width=cur.width, height=cur.height))
        package = fp.package
        if not package:
            return None
        if self.workflow is None and package != self._selected_for:
            await self._select(package, live_fp=fp)
        launched_note = ""
        launcher_pkg = await self._launcher_package() if (self.workflow is None and not self._tried_cross_app) else None
        is_home = bool(launcher_pkg and package == launcher_pkg)
        if self.workflow is None and is_home and not self._tried_cross_app and self.autopilot_steps == 0:
            self._tried_cross_app = True
            all_idx = [e for e in self.store.list_all_index() if e.has_runnable]
            if all_idx:
                entry, values = select_workflow_fast(self.runner.goal, all_idx)
                if entry is not None and entry.package != package:
                    loaded = self.store.load(entry.package, entry.workflow_id)
                    if loaded is not None:
                        self.workflow, self.slot_values = loaded, values
                        self._autopilot = Autopilot(loaded, values, max_steps=self.max_steps)
                        self._bytes, self._xml = img_bytes, xml_list
                        ok_launch, _ = await _Driver(self, state).act(
                            "manage_app", {"action": "launch", "app_name": loaded.package}, None
                        )
                        if ok_launch:
                            launched_note = f"manage_app launch {loaded.package} -> ok"
                            self.autopilot_steps += 1
                            self._done_steps.append(launched_note)
                            package = loaded.package
                            logger.info(
                                f"Workflow launcher auto-open: {loaded.workflow_id} ({loaded.package}) slots={values}"
                            )
                        else:
                            self.workflow, self.slot_values, self._autopilot = None, {}, None
                            return None
        if self._autopilot is None or self.workflow is None or package != self.workflow.package:
            return None
        self._bytes, self._xml = img_bytes, xml_list
        rep = await self._autopilot.run(_Driver(self, state))
        if launched_note:
            rep.ran = [launched_note] + rep.ran
            rep.acted += 1
        logger.info(
            f"Autopilot: {rep.steps} step(s) in {rep.elapsed_s}s; stopped: {rep.stop_reason}"
        )
        if rep.steps > 0:
            self.autopilot_steps += rep.steps
            self._done_steps.extend(rep.ran)
            self._publish_session_workflow()
        elif self.workflow is not None:
            self._publish_session_workflow()
        if rep.acted == 0 and rep.finished and "finished" not in self._revisit_warned:
            # Back on the end screen without acting (e.g. the AI did the last
            # pick itself): say once that the recorded run finished here.
            self._revisit_warned.add("finished")
            self._arm_end_guard(rep)
            rep.ran = [f"(earlier in this run) {d}" for d in self._done_steps] or rep.ran
            notice = rep.notice(
                self.workflow.workflow_id, scrollable=bool(getattr(state, "screen_scrollable", False))
            )
            self._playbook_sent = True
            return img_bytes, xml_list, notice
        if rep.acted == 0:
            # Nothing touched the device: the turn's observation stands. Back on
            # a screen this run already got past (the list the task started
            # from): warn once, before the model redoes the steps to "check".
            notices: list[str] = []
            if not self._playbook_sent:
                pb = build_playbook(
                    self.workflow,
                    self.slot_values,
                    done_edge_ids=rep.done_edge_ids,
                    show_progress=bool(rep.done_edge_ids),
                )
                if pb:
                    notices.append(pb)
                self._playbook_sent = True
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
                notices.append(REVISIT_NOTICE.format(steps=steps or "recorded", scroll=scroll))
            if notices:
                return img_bytes, xml_list, "\n".join(notices)
            return None
        if launched_note and rep.steps == 0:
            self.autopilot_steps += rep.steps
            self._done_steps.extend(rep.ran)
        if rep.finished:
            self._revisit_warned.add("finished")
            self._arm_end_guard(rep)
            if not rep.ran and self._done_steps:
                rep.ran = [f"(earlier in this run) {d}" for d in self._done_steps]
        new_bytes, new_xml = await self.runner._reobserve_after_resume(state)
        self.runner._force_image = True
        scrollable = bool(getattr(state, "screen_scrollable", False))
        notice = rep.notice(self.workflow.workflow_id, scrollable=scrollable)
        if not rep.finished:
            if not self._playbook_sent:
                pb = build_playbook(
                    self.workflow,
                    self.slot_values,
                    done_edge_ids=rep.done_edge_ids,
                    show_progress=True,
                )
                if pb:
                    notice += "\n" + pb
                self._playbook_sent = True
            else:
                nxt = next_playbook_step(self.workflow, self.slot_values, rep.done_edge_ids)
                if nxt:
                    notice += f"\n[Playbook] Next step: {nxt}."
        return new_bytes, new_xml, notice
