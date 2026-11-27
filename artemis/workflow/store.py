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

"""Workflow graph model and on-disk store (KE_HOACH_APP_WORKFLOW Mục 3.1, 3.3).

One JSON file per workflow under ``<app_dir>/workflows/<package>/``. Writes are
atomic (temp file + rename) and keep one ``.bak`` of the previous version.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import time
from typing import Any

from artemis.workflow.fingerprint import ScreenFingerprint
from artemis.workflow.targets import TargetDescriptor

SCHEMA = 2
MAX_FPS_PER_NODE = 6
EDGE_KINDS = ("nav", "slot", "judgement", "commitment_stop")


def _tupleize(v: Any) -> Any:
    return tuple(_tupleize(x) for x in v) if isinstance(v, list) else v


def _listify(v: Any) -> Any:
    return [_listify(x) for x in v] if isinstance(v, (list, tuple)) else v


def fp_to_json(fp: ScreenFingerprint) -> dict[str, Any]:
    return {
        "package": fp.package,
        "tokens": [[_listify(tok), cnt] for tok, cnt in fp.tokens.items()],
        "app_token_count": fp.app_token_count,
        "resource_ids": sorted(fp.resource_ids),
        "node_count": fp.node_count,
    }


def fp_from_json(d: dict[str, Any]) -> ScreenFingerprint:
    return ScreenFingerprint(
        package=str(d.get("package") or ""),
        tokens=Counter({_tupleize(tok): int(cnt) for tok, cnt in d.get("tokens") or []}),
        app_token_count=int(d.get("app_token_count") or 0),
        resource_ids=frozenset(d.get("resource_ids") or []),
        node_count=int(d.get("node_count") or 0),
    )


@dataclass
class Node:
    id: str
    fps: list[ScreenFingerprint] = field(default_factory=list)

    def add_fp(self, fp: ScreenFingerprint) -> None:
        if any(f.tokens == fp.tokens and f.package == fp.package for f in self.fps):
            return  # same layout already known
        self.fps.append(fp)
        if len(self.fps) > MAX_FPS_PER_NODE:
            # Keep the first one: the state the screen is entered in (before
            # scrolling / typing), which the next run will see first.
            self.fps = self.fps[:1] + self.fps[-(MAX_FPS_PER_NODE - 1):]


@dataclass
class Edge:
    """One recorded transition. ``kind``:

    - ``nav``: same action every time (open a screen, press search) -> autopilot;
    - ``slot``: action parametrized by a task value (type / pick ``{slot}``) -> autopilot
      when the slot has a value for the new task;
    - ``judgement``: a choice that depends on what is on screen -> always the AI;
    - ``commitment_stop``: an irreversible action was next -> autopilot never passes.
    """

    id: str
    src: str
    dst: str | None
    kind: str
    tool: str = ""
    key: str = ""  # press_key
    slot: str = ""  # slot whose value the action types or picks
    typed: str = ""  # precondition: input fields already hold these slots' values (comma-separated)
    target: TargetDescriptor | None = None
    anchors: list[dict[str, str]] = field(default_factory=list)
    ok: int = 0
    fail: int = 0
    consec_fail: int = 0
    state: str = "active"  # active | disabled

    def signature(self) -> tuple[Any, ...]:
        t = self.target
        return (
            self.src,
            self.kind,
            self.tool,
            self.key,
            self.slot,
            self.typed,
            (t.resource_id, t.hint, t.text, t.slot, t.focused_editable, t.grid) if t else None,
        )

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "from": self.src,
            "to": self.dst,
            "kind": self.kind,
            "tool": self.tool,
            "stats": {"ok": self.ok, "fail": self.fail, "consec_fail": self.consec_fail},
            "state": self.state,
        }
        if self.key:
            d["key"] = self.key
        if self.slot:
            d["slot"] = self.slot
        if self.typed:
            d["typed"] = self.typed
        if self.target is not None:
            d["target"] = self.target.to_dict()
        if self.anchors:
            d["anchors"] = self.anchors
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Edge":
        st = d.get("stats") or {}
        return cls(
            id=str(d["id"]),
            src=str(d["from"]),
            dst=d.get("to"),
            kind=str(d.get("kind") or "judgement"),
            tool=str(d.get("tool") or ""),
            key=str(d.get("key") or ""),
            slot=str(d.get("slot") or ""),
            typed=str(d.get("typed") or ""),
            target=TargetDescriptor.from_dict(d["target"]) if d.get("target") else None,
            anchors=list(d.get("anchors") or []),
            ok=int(st.get("ok", 0)),
            fail=int(st.get("fail", 0)),
            consec_fail=int(st.get("consec_fail", 0)),
            state=str(d.get("state") or "active"),
        )


@dataclass
class Workflow:
    workflow_id: str
    package: str
    goal: str
    slots: dict[str, str] = field(default_factory=dict)  # slot name -> value in ``goal``
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    runs: int = 0
    ok_runs: int = 0
    updated_at: float = 0.0
    end_nodes: list[str] = field(default_factory=list)  # screens a successful run finished on

    def new_node_id(self) -> str:
        i = len(self.nodes) + 1
        while f"n{i}" in self.nodes:
            i += 1
        return f"n{i}"

    def new_edge_id(self) -> str:
        used = {e.id for e in self.edges}
        i = len(self.edges) + 1
        while f"e{i}" in used:
            i += 1
        return f"e{i}"

    def node_fps(self) -> dict[str, list[ScreenFingerprint]]:
        return {nid: n.fps for nid, n in self.nodes.items() if n.fps}

    def edges_from(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.src == node_id]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "workflow_id": self.workflow_id,
            "package": self.package,
            "goal": self.goal,
            "slots": self.slots,
            "stats": {"runs": self.runs, "ok": self.ok_runs},
            "updated_at": self.updated_at,
            "nodes": {nid: {"fingerprints": [fp_to_json(f) for f in n.fps]} for nid, n in self.nodes.items()},
            "edges": [e.to_json() for e in self.edges],
            "end_nodes": self.end_nodes,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Workflow":
        st = d.get("stats") or {}
        return cls(
            workflow_id=str(d["workflow_id"]),
            package=str(d["package"]),
            goal=str(d.get("goal") or ""),
            slots={str(k): str(v) for k, v in (d.get("slots") or {}).items()},
            nodes={
                nid: Node(nid, [fp_from_json(f) for f in (nd.get("fingerprints") or [])])
                for nid, nd in (d.get("nodes") or {}).items()
            },
            edges=[Edge.from_json(e) for e in d.get("edges") or []],
            runs=int(st.get("runs", 0)),
            ok_runs=int(st.get("ok", 0)),
            updated_at=float(d.get("updated_at") or 0.0),
            end_nodes=[str(n) for n in d.get("end_nodes") or []],
        )


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "app"


class WorkflowStore:
    """Directory of workflow JSON files, one folder per app package."""

    def __init__(self, root: Path | str | None = None):
        if root is None:
            from artemis.config.paths import get_app_dir

            root = get_app_dir() / "workflows"
        self.root = Path(root)

    def _dir(self, package: str) -> Path:
        return self.root / _safe(package)

    def list(self, package: str) -> list[Workflow]:
        d = self._dir(package)
        if not d.is_dir():
            return []
        out: list[Workflow] = []
        for p in sorted(d.glob("*.json")):
            try:
                wf = Workflow.from_json(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if wf.package == package:
                out.append(wf)
        return out

    def new_id(self, package: str) -> str:
        d = self._dir(package)
        i = 1
        while (d / f"{_safe(package)}__{i}.json").exists():
            i += 1
        return f"{_safe(package)}__{i}"

    def save(self, wf: Workflow) -> Path:
        d = self._dir(wf.package)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_safe(wf.workflow_id)}.json"
        wf.updated_at = time.time()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wf.to_json(), ensure_ascii=False, indent=1), encoding="utf-8")
        if path.exists():
            os.replace(path, path.with_suffix(".json.bak"))
        os.replace(tmp, path)
        return path
