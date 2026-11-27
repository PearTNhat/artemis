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

"""Run-level loop breaker for the Flash runner.

A weak model can redo the same steps forever (re-add an item it does not see,
re-tap a hint label). Prompts and notices do not stop it, so this is enforced
in code: every model action gets a signature = (screen *content and element
states*, tool, target point / key / typed text, and the same of the action
before it). The same signature a 2nd time -> warning notice; a 3rd time -> the
run stops as failed with the reason.

The screen part is its content (labels of the foreground app), not just its
layout, so legitimate repetition that changes what is shown (tap "+" to raise a
quantity, swipe down a list, type into successive fields) never matches.
"""

from __future__ import annotations

import hashlib
from typing import Any

from artemis.utils.screen_diff import flatten_hierarchy
from artemis.workflow.fingerprint import foreground_package, normalize
from artemis.workflow.targets import node_label, visible_app_nodes

WARN_AT = 2
STOP_AT = 3
_IGNORED = frozenset(
    {
        "wait_for_text",
        "wait_until_gone",
        "wait_for_screen_change",
        "wait_for_delay",
        "wait",
        "ask_user_choice",
        "report_task_status",
    }
)


def screen_content_key(raw: dict[str, Any] | None) -> str | None:
    """Hash of the foreground app's visible labels AND element states (checked,
    selected, enabled, error): picking an option changes the key even when no
    text changes. None when there is no usable tree."""
    if not isinstance(raw, dict) or not raw.get("elements"):
        return None
    norm = normalize(raw["elements"], width=raw.get("width"), height=raw.get("height"))
    fg = foreground_package(norm)
    labels = sorted(
        node_label(n) for n in visible_app_nodes(norm) if node_label(n) and (not fg or n.get("package") == fg)
    )
    if not labels:
        return None
    states = []
    for n in flatten_hierarchy(raw["elements"]) or []:
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


def _pt(p: Any) -> str:
    return f"{int(p[0]) // 25},{int(p[1]) // 25}"


def action_key(name: str, args: dict[str, Any], resolved_point: Any = None) -> str:
    args = args or {}
    point = resolved_point if isinstance(resolved_point, (list, tuple)) and len(resolved_point) == 2 else None
    if point is None:
        tgt = args.get("target") or args.get("coordinates")
        if isinstance(tgt, (list, tuple)) and len(tgt) == 2:
            point = tgt
    pt = _pt(point) if point is not None else ""
    seq = args.get("sequence")
    if isinstance(seq, list):
        pt = ";".join(_pt(p) for p in seq if isinstance(p, (list, tuple)) and len(p) == 2)
    extra = str(args.get("text") or args.get("key") or args.get("direction") or args.get("app_name") or "")
    return f"{name}|{pt}|{extra.strip().casefold()}"


class LoopGuard:
    def __init__(self, warn_at: int = WARN_AT, stop_at: int = STOP_AT):
        self.warn_at = warn_at
        self.stop_at = stop_at
        self.counts: dict[str, int] = {}
        self.prev = ""
        self.notice: str | None = None
        self.stop_reason: str | None = None

    def note(self, name: str, args: dict[str, Any], pre_raw: dict[str, Any] | None, resolved_point: Any = None) -> None:
        """Counts one executed model action taken on the screen ``pre_raw``."""
        if name in _IGNORED or self.stop_reason:
            return
        screen = screen_content_key(pre_raw)
        if screen is None:
            return
        act = action_key(name, args, resolved_point)
        # The step before is part of the key: a real loop repeats the whole
        # path (same step after the same step); fixing a form and tapping its
        # confirm button again comes after a different step.
        key = f"{screen}|{act}|after:{self.prev}"
        self.prev = act  # the previous step's action only: its screen may hold a ticking clock
        n = self.counts.get(key, 0) + 1
        self.counts[key] = n
        what = f"{name} {action_key(name, args, resolved_point).split('|', 1)[1]}".strip()
        if n >= self.stop_at:
            self.stop_reason = (
                f"Loop detected: the same action ({what}) was taken {n} times on the same screen content"
                " without getting anywhere; the run was stopped instead of looping forever."
            )
        elif n >= self.warn_at:
            self.notice = (
                f"[Loop warning] You already did exactly this ({what}) on this same screen earlier in"
                " this run. Repeating it will not change the outcome: if the task is already done (an"
                " item you added may be further down a scrolling list), verify and call"
                " report_task_status; otherwise try something different. One more identical"
                " repetition stops the run."
            )

    def pop_notice(self) -> str | None:
        n, self.notice = self.notice, None
        return n
