"""Where a task's time went: a per-step timing report for one session.

Reads the DataEngine database (steps + trace spans) and splits every step's
wall time into model wait, device action, post-action screen capture and
prompt preparation, using the ``timing`` the Flash runner stamps on each step.
Background agents that ran inside an action (e.g. the Hopper resolving an app
name during ``manage_app``) are listed from their trace spans.

Usage::

    uv run python -m artemis.utils.timing_report            # latest session
    uv run python -m artemis.utils.timing_report <session_id-or-prefix>
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


def _connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_session(conn: sqlite3.Connection, session: str | None) -> sqlite3.Row | None:
    if not session or session == "latest":
        return conn.execute("SELECT * FROM sessions ORDER BY start_time DESC LIMIT 1").fetchone()
    return conn.execute(
        "SELECT * FROM sessions WHERE session_id LIKE ? ORDER BY start_time DESC LIMIT 1",
        (f"{session}%",),
    ).fetchone()


def _target(action: dict[str, Any]) -> str:
    args = action.get("args") or {}
    value = (
        action.get("target_text")
        or action.get("target_description")
        or args.get("text")
        or args.get("app_name")
        or args.get("direction")
        or args.get("key")
        or ""
    )
    return str(value)


def build_timing_report(db_path: Path | str, session: str | None = None) -> dict[str, Any] | None:
    """Collects the per-step timing of a session (``None`` if it does not exist)."""
    with _connect(db_path) as conn:
        row = _resolve_session(conn, session)
        if row is None:
            return None
        sid = row["session_id"]
        start = float(row["start_time"] or 0)
        steps_rows = conn.execute(
            "SELECT step_number, timestamp, action_taken, extra_metadata FROM steps"
            " WHERE session_id = ? ORDER BY step_number",
            (sid,),
        ).fetchall()
        span_rows = conn.execute(
            "SELECT type, name, COUNT(*) AS n, SUM(duration) AS total, MAX(duration) AS longest"
            " FROM traces WHERE session_id = ? AND duration IS NOT NULL AND duration > 0"
            " AND type IN ('agent', 'span') AND name NOT IN ('FlashRunner')"
            " GROUP BY type, name ORDER BY total DESC",
            (sid,),
        ).fetchall()

    steps: list[dict[str, Any]] = []
    previous_ts = start
    for r in steps_rows:
        action = json.loads(r["action_taken"] or "{}")
        meta = json.loads(r["extra_metadata"] or "{}")
        timing = meta.get("timing") or {}
        usage = meta.get("token_usage") or {}
        ts = float(r["timestamp"] or previous_ts)
        steps.append(
            {
                "step": r["step_number"],
                "action": action.get("action") or "?",
                "target": _target(action),
                "wall_s": round(max(0.0, ts - previous_ts), 2),
                "llm_s": timing.get("llm_s"),
                "action_s": timing.get("action_s"),
                "observe_s": timing.get("observe_s"),
                "prep_s": timing.get("prep_s"),
                "shared_model_call": bool(timing.get("shared_model_call")),
                "llm_resumes": timing.get("llm_resumes") or 0,
                "agy": timing.get("agy"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
            }
        )
        previous_ts = ts

    def total(key: str) -> float:
        return round(sum(float(s[key] or 0) for s in steps), 2)

    wall = round(sum(s["wall_s"] for s in steps), 2)
    totals = {
        "wall_s": wall,
        "llm_s": total("llm_s"),
        "action_s": total("action_s"),
        "observe_s": total("observe_s"),
        "prep_s": total("prep_s"),
    }
    measured = totals["llm_s"] + totals["action_s"] + totals["observe_s"] + totals["prep_s"]
    totals["other_s"] = round(max(0.0, wall - measured), 2)
    return {
        "session_id": sid,
        "goal": row["initial_goal"],
        "status": row["status"],
        "has_timing": any(s["llm_s"] is not None or s["action_s"] is not None for s in steps),
        "steps": steps,
        "totals": totals,
        "agents": [
            {
                "name": s["name"],
                "type": s["type"],
                "count": s["n"],
                "total_s": round(s["total"] or 0, 2),
                "longest_s": round(s["longest"] or 0, 2),
            }
            for s in span_rows
        ],
    }


def _fmt(value: Any) -> str:
    return "-" if value is None else f"{float(value):.1f}"


def format_timing_report(report: dict[str, Any]) -> str:
    """Plain-text table for the terminal."""
    lines = [
        f"Session {report['session_id']} [{report['status']}]",
        f"Goal: {str(report['goal']).splitlines()[0][:100]}",
        "",
        f"{'#':>3} {'wall':>6} {'model':>6} {'action':>6} {'screen':>6} {'prep':>5}  {'tok in/out':>11}  action",
    ]
    for s in report["steps"]:
        model = "(same)" if s["shared_model_call"] else _fmt(s["llm_s"])
        tokens = f"{s['prompt_tokens'] or '-'}/{s['completion_tokens'] or '-'}"
        note = ""
        if s["agy"]:
            agy = s["agy"]
            if agy.get("transport") == "acp":
                note = f"  [agy acp {agy.get('model_calls')} calls, {agy.get('inline_images', 0)} inline image(s)]"
            else:
                note = f"  [agy {agy.get('model_calls')} calls, screenshot {'yes' if agy.get('opened_screenshot') else 'no'}]"
        if s["llm_resumes"]:
            note += f"  [resumed {s['llm_resumes']}x]"
        lines.append(
            f"{s['step']:>3} {s['wall_s']:>6.1f} {model:>6} {_fmt(s['action_s']):>6}"
            f" {_fmt(s['observe_s']):>6} {_fmt(s['prep_s']):>5}  {tokens:>11}"
            f"  {s['action']}({s['target'][:40]}){note}"
        )
    t = report["totals"]
    wall = t["wall_s"] or 1.0

    def pct(v: float) -> str:
        return f"{v:.1f}s ({100 * v / wall:.0f}%)"

    lines += [
        "",
        f"Total wall time {t['wall_s']:.1f}s:",
        f"  model wait     {pct(t['llm_s'])}",
        f"  device actions {pct(t['action_s'])}",
        f"  screen capture {pct(t['observe_s'])}",
        f"  prompt prep    {pct(t['prep_s'])}",
        f"  other/unmeasured {pct(t['other_s'])}",
    ]
    if not report["has_timing"]:
        lines.append(
            "  (this session ran before per-step timing was recorded: only wall time is known)"
        )
    if report["agents"]:
        lines += ["", "Helper agents / spans (time inside actions):"]
        for a in report["agents"]:
            lines.append(
                f"  {a['name']:<30} x{a['count']:<3} total {a['total_s']:.1f}s, longest {a['longest_s']:.1f}s"
            )
    slow = sorted(report["steps"], key=lambda s: s["wall_s"], reverse=True)[:3]
    if slow:
        lines += ["", "Slowest steps: " + ", ".join(f"#{s['step']} {s['wall_s']:.0f}s" for s in slow)]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from artemis.config import DB_PATH

    args = sys.argv[1:] if argv is None else argv
    report = build_timing_report(DB_PATH, args[0] if args else None)
    if report is None:
        print("Session not found.")
        return 1
    print(format_timing_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
