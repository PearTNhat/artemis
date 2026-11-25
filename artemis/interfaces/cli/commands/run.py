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

"""Task execution command (artemis run)."""

import asyncio
import os
import select
import sys
import threading
import time
from typing import Annotated

from langchain_core.callbacks.base import Callbacks
from artemis.config import checker_overrides_for_level, settings
from artemis.utils.startup_progress import publish_startup_progress
from third_party.mobile_use.main import (
    GoalArgument,
    OutputDescriptionOption,
    TestNameOption,
    TracesPathOption,
    VideoRecordingToolsOption,
    ensure_video_recording_available,
    new_default_config_builder,
    run_automation,
)
from third_party.mobile_use.utils.cli_helpers import display_local_device_status
from third_party.mobile_use.utils.logger import get_logger
import signal
from rich.console import Console
from rich.panel import Panel
import typer

logger = get_logger(__name__)


async def execute_task(
    goal: str,
    device_serial: str | None = None,
    session_id: str | None = None,
    locked_app_package: str | None = None,
    test_name: str | None = None,
    traces_output_path_str: str | None = None,
    output_description: str | None = None,
    graph_config_callbacks: Callbacks = [],
    video_recording_tools_enabled: bool | None = None,
    profile: str | None = None,
    app_path: str | None = None,
    enable_planner_validation: bool | None = None,
    enable_committee: bool | None = None,
    enable_checker: bool | None = None,
    enable_step_summarizer: bool | None = None,
    enable_outputter: bool | None = None,
    force_output_synthesis: bool | None = None,
    explorer_version: str | None = None,
    explorer_flash_mode: str | None = None,
    explorer_pro_mode: str | None = None,
    verification_level: str | None = None,
    no_ask_user: bool = False,
    ask_on_choice: bool | None = None,
    ask_when_stuck: bool | None = None,
) -> None:
    """Executes a single mobile automation task end-to-end.

    Args:
        goal: Target objective to achieve on the mobile device.
        locked_app_package: Optional package name to constrain actions to.
        test_name: Optional test identifier for trace directory naming.
        traces_output_path_str: Destination path for recording traces.
        output_description: Structured natural language output description.
        graph_config_callbacks: LangGraph callback handlers.
        video_recording_tools_enabled: Whether screen video analyzer is enabled.
        profile: Execution profile ('flash' for fast reactive loop, 'pro' for full graph).
        app_path: Optional local APK path to install prior to execution.
        enable_planner_validation: Explicit override to enable/disable async planner validation.
        enable_committee: Explicit override to enable/disable Multi-Agent Committee tool.
        explorer_version: Override the fallback Explorer tier ('flash', 'pro', or 'ultra').
        explorer_flash_mode: Override the Explorer tier for the Flash execution profile.
        explorer_pro_mode: Override the Explorer tier for the Pro execution profile.
        verification_level: Coarse Checker preset ('off', 'final', 'checkpoints',
            'strict'); applied before the explicit ``enable_checker`` switch.
    """
    effective_sid = (
        session_id or os.getenv("ARTEMIS_SESSION_ID") or os.getenv("ARTEMIS_CLOUD_SESSION_ID")
    )
    if effective_sid:
        os.environ["ARTEMIS_SESSION_ID"] = str(effective_sid)
    if not os.environ.get("ARTEMIS_TASK_INGRESS"):
        os.environ["ARTEMIS_TASK_INGRESS"] = "cli"
    publish_startup_progress(
        "configuration",
        "Loading the run configuration",
        session_id=str(effective_sid) if effective_sid else None,
    )

    config = new_default_config_builder()

    if video_recording_tools_enabled is not None:
        config.with_video_recording_tools(enabled=video_recording_tools_enabled)

    if enable_planner_validation is not None:
        config.with_planner_validation(enabled=enable_planner_validation)

    if enable_committee is not None:
        config.with_committee(enabled=enable_committee)

    if verification_level is not None:
        config.with_checker(**checker_overrides_for_level(verification_level))

    if enable_checker is not None:
        config.with_checker(enabled=enable_checker)

    if enable_step_summarizer is not None:
        config.with_flash_step_summarizer(enabled=enable_step_summarizer)

    if enable_outputter is not None or force_output_synthesis is not None:
        config.with_outputter(
            enabled=enable_outputter if enable_outputter is not None else True,
            force_synthesis=bool(force_output_synthesis),
        )

    if (
        explorer_version is not None
        or explorer_flash_mode is not None
        or explorer_pro_mode is not None
    ):
        config.with_explorer(
            version=explorer_version,
            flash_mode=explorer_flash_mode,
            pro_mode=explorer_pro_mode,
        )

    if no_ask_user or ask_on_choice is not None or ask_when_stuck is not None:
        config.with_flash_user_choice(
            enabled=False if no_ask_user else None,
            ask_on_choice=ask_on_choice,
            ask_when_stuck=ask_when_stuck,
        )

    is_worker = (
        os.environ.get("ARTEMIS_TASK_WORKER") == "1"
        or os.environ.get("ARTEMIS_DEVICE_QUEUE_TICKET") is not None
    )
    use_cli_prompt = (not is_worker) and (not no_ask_user) and sys.stdin.isatty()

    target_serial = (
        device_serial or settings.ADB_DEVICE_SERIAL or os.environ.get("ADB_DEVICE_SERIAL")
    )
    if not target_serial:
        try:
            from artemis.runtime import device_pool

            target_serial = device_pool.select_device()
        except Exception:
            target_serial = None

    if target_serial:
        from artemis.context import DevicePlatform

        config.for_device(DevicePlatform.ANDROID, target_serial)

    if graph_config_callbacks:
        config.with_graph_config_callbacks(graph_config_callbacks)

    from artemis.agents.flash.user_choice import (
        format_pending_choice_prompt,
        set_cli_choice_prompt,
    )

    _CHOICE_INPUT_PROMPT = "Chọn [số / a=auto / làm xong rồi gõ m=manual / chữ khác]: "

    if use_cli_prompt:
        def _terminal_choice_prompt(pending: dict, stop_event: threading.Event) -> str | None:
            print(f"\n{format_pending_choice_prompt(pending)}", flush=True)
            print(_CHOICE_INPUT_PROMPT, end="", flush=True)
            return _poll_stdin_line(
                should_stop_fn=stop_event.is_set,
                poll_interval=0.2,
                reprompt_line=_CHOICE_INPUT_PROMPT,
            )

        set_cli_choice_prompt(_terminal_choice_prompt)
    else:
        set_cli_choice_prompt(None)

    try:
        await run_automation(
            config,
            goal,
            session_id=effective_sid,
            locked_app_package=locked_app_package,
            test_name=test_name,
            traces_output_path_str=traces_output_path_str,
            output_description=output_description,
            profile=profile,
            app_path=app_path,
        )
    finally:
        set_cli_choice_prompt(None)


def _resolve_daemon_user_choice_flags(
    *,
    no_ask_user: bool,
    ask_on_choice: bool | None,
    ask_when_stuck: bool | None,
    stdin_isatty: bool,
) -> tuple[bool | None, bool | None]:
    """Return ``(ask_on_choice, ask_when_stuck)`` for daemon submission.

    When ``--no-ask-user`` is set or stdin is not an interactive TTY (scripts/CI),
    force both switches to ``False`` so the daemon never pauses waiting on input.
    """
    if no_ask_user or not stdin_isatty:
        return False, False
    return ask_on_choice, ask_when_stuck


def _poll_stdin_line(
    *,
    should_stop_fn,
    poll_interval: float,
    reprompt_line: str,
    console: Console | None = None,
) -> str | None:
    """Read a non-empty line from ``sys.stdin``, checking ``should_stop_fn`` between polls."""
    if sys.platform != "win32" and hasattr(select, "select"):
        while not should_stop_fn():
            try:
                ready, _, _ = select.select([sys.stdin], [], [], poll_interval)
            except (ValueError, OSError):
                return None
            if should_stop_fn():
                return None
            if ready:
                line = sys.stdin.readline()
                if not line:
                    return None
                if line.strip():
                    return line.strip()
                if console is not None:
                    console.print(f"[dim]{reprompt_line}[/dim]", end="")
                else:
                    print(reprompt_line, end="", flush=True)
        return None

    while not should_stop_fn():
        try:
            raw = input()
        except EOFError:
            return None
        if should_stop_fn():
            return None
        if raw.strip():
            return raw.strip()
        if console is not None:
            console.print(f"[dim]{reprompt_line}[/dim]", end="")
        else:
            print(reprompt_line, end="", flush=True)
    return None


def _read_cli_choice_answer(
    session_id: str,
    pending: dict,
    *,
    base_url: str,
    console: Console,
    get_pending_fn,
    submit_choice_fn,
    parse_input_fn,
    poll_interval: float,
    stdin_readline_fn=None,
) -> None:
    """Wait for a single CLI answer in daemon mode unless resolved externally on Web UI."""
    choice_id = str(pending.get("choice_id") or "")
    prompt_line = "Chọn [số / a=auto / làm xong rồi gõ m=manual / chữ khác]: "
    if stdin_readline_fn is not None:
        while True:
            raw = stdin_readline_fn(prompt_line)
            if raw is None:
                return
            parsed = parse_input_fn(raw, pending)
            if parsed is not None:
                submit_choice_fn(session_id, parsed, base_url=base_url)
                return

    resolved_externally = {"flag": False}

    def _external_resolved() -> bool:
        latest = get_pending_fn(session_id, base_url=base_url)
        if not latest or str(latest.get("choice_id") or "") != choice_id:
            resolved_externally["flag"] = True
            return True
        return False

    console.print(f"[dim]{prompt_line}[/dim]", end="")
    raw_line = _poll_stdin_line(
        should_stop_fn=_external_resolved,
        poll_interval=poll_interval,
        reprompt_line=prompt_line,
        console=console,
    )
    if resolved_externally["flag"]:
        console.print("\n[dim]✓ Đã được trả lời qua giao diện khác, tiếp tục chạy...[/dim]")
        return
    if raw_line is None:
        return
    parsed = parse_input_fn(raw_line, pending)
    if parsed is not None:
        submit_choice_fn(session_id, parsed, base_url=base_url)
        console.print("[green]✓ Đã gửi lựa chọn cho AI.[/green]")


def _wait_daemon_with_interactive_choices(
    session_id: str,
    *,
    base_url: str,
    console: Console,
    interactive_tty: bool,
    get_session_fn,
    get_status_fn,
    get_pending_fn,
    submit_choice_fn,
    format_prompt_fn,
    parse_input_fn,
    on_status_update=None,
    timeout: float = 1800.0,
    poll_interval: float = 1.0,
    stdin_readline_fn=None,
    clock_fn=None,
    sleep_fn=None,
) -> dict:
    """Wait for daemon task completion while surfacing and handling pending user choices.

    Time spent awaiting a user choice is excluded from ``timeout`` so waiting for
    human input stays unbounded.
    """
    now_fn = clock_fn or time.monotonic
    wait_fn = sleep_fn or time.sleep
    active_elapsed = 0.0
    last_tick = now_fn()
    was_awaiting = False
    last_status: str | None = None
    prompted_choice_id: str | None = None

    while active_elapsed < timeout:
        now = now_fn()
        if not was_awaiting:
            active_elapsed += max(0.0, now - last_tick)
        last_tick = now
        if active_elapsed >= timeout:
            break

        sess = get_session_fn(session_id, base_url=base_url)
        if sess:
            status = sess.get("status")
            if status != last_status:
                last_status = status
                if callable(on_status_update):
                    on_status_update(sess)
            if status in ("completed", "success", "failed", "cancelled"):
                return sess
        else:
            st = get_status_fn(base_url=base_url)
            if st:
                queue = st.get("queue") or []
                is_queued = any(
                    isinstance(item, dict) and str(item.get("session_id")) == str(session_id)
                    for item in queue
                )
                current_st = "queued" if is_queued else "launching"
                if current_st != last_status:
                    last_status = current_st
                    if callable(on_status_update):
                        on_status_update({"status": current_st, "session_id": session_id})

        pending = get_pending_fn(session_id, base_url=base_url)
        was_awaiting = pending is not None
        if pending:
            cid = str(pending.get("choice_id") or "")
            if cid and cid != prompted_choice_id:
                prompted_choice_id = cid
                console.print(f"\n[yellow]{format_prompt_fn(pending)}[/yellow]")
                console.print(f"[dim]Có thể trả lời tại Web UI ({base_url}) hoặc ngay tại đây.[/dim]")
                if interactive_tty:
                    _read_cli_choice_answer(
                        session_id,
                        pending,
                        base_url=base_url,
                        console=console,
                        get_pending_fn=get_pending_fn,
                        submit_choice_fn=submit_choice_fn,
                        parse_input_fn=parse_input_fn,
                        poll_interval=poll_interval,
                        stdin_readline_fn=stdin_readline_fn,
                    )
                    # Exclude time blocked reading CLI choice input from active_elapsed.
                    last_tick = now_fn()
                    was_awaiting = get_pending_fn(session_id, base_url=base_url) is not None
        wait_fn(poll_interval)

    return {"session_id": session_id, "status": "timeout", "error": f"Timed out after {timeout}s"}


def run_command(
    goal: GoalArgument,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            "-p",
            help="Execution profile ('flash' for fast reactive, 'pro' for full graph).",
        ),
    ] = "pro",
    locked_app_package: Annotated[
        str | None,
        typer.Option(
            "--locked-app",
            "-a",
            help="Optional Android package name to restrict the agent to.",
        ),
    ] = None,
    test_name: TestNameOption = None,
    traces_path: TracesPathOption = None,
    output_description: OutputDescriptionOption = None,
    with_video_recording_tools: VideoRecordingToolsOption = None,
    app_path: Annotated[
        str | None,
        typer.Option(
            "--app-path",
            help="Local APK path to install before starting the task.",
        ),
    ] = None,
    enable_planner_validation: Annotated[
        bool | None,
        typer.Option(
            "--enable-planner-validation/--disable-planner-validation",
            help="Enable or disable the advisory Planner review of task plan milestone edits (hint only, never rolled back).",
        ),
    ] = None,
    enable_committee: Annotated[
        bool | None,
        typer.Option(
            "--enable-committee/--disable-committee",
            help="Enable or disable Multi-Agent Committee council debate tool for Operator.",
        ),
    ] = None,
    enable_checker: Annotated[
        bool | None,
        typer.Option(
            "--enable-checker/--disable-checker",
            help="Enable or disable the Checker (plan checkpoint verification and exit final review).",
        ),
    ] = None,
    enable_step_summarizer: Annotated[
        bool | None,
        typer.Option(
            "--enable-step-summarizer/--disable-step-summarizer",
            help="Enable or disable Flash asynchronous objective visual step state summarizer.",
        ),
    ] = None,
    enable_outputter: Annotated[
        bool | None,
        typer.Option(
            "--enable-outputter/--disable-outputter",
            help="Enable or disable Outputter post-execution report and structured output synthesis.",
        ),
    ] = None,
    force_output_synthesis: Annotated[
        bool | None,
        typer.Option(
            "--force-output-synthesis/--no-force-output-synthesis",
            help="Force Outputter report synthesis even if no structured output schema is specified.",
        ),
    ] = None,
    explorer_version: Annotated[
        str | None,
        typer.Option(
            "--explorer-version",
            help=(
                "Fallback Explorer tier when no profile knob applies: 'flash' (one-shot"
                " detection), 'pro' (3-turn reasoning loop) or 'ultra' (8-turn loop with"
                " zoom, OCR and image processing)."
            ),
        ),
    ] = None,
    explorer_flash_mode: Annotated[
        str | None,
        typer.Option(
            "--explorer-flash-mode",
            help="Explorer tier behind ask_explorer under the Flash profile ('flash', 'pro', 'ultra').",
        ),
    ] = None,
    explorer_pro_mode: Annotated[
        str | None,
        typer.Option(
            "--explorer-pro-mode",
            help="Explorer tier behind ask_explorer under the Pro profile ('flash', 'pro', 'ultra').",
        ),
    ] = None,
    verification_level: Annotated[
        str | None,
        typer.Option(
            "--verification-level",
            help=(
                "Checker preset for the Pro profile: 'off' (no audit), 'final' (exit review"
                " only, the default), 'checkpoints' (every plan checkpoint + exit review),"
                " 'strict' (checkpoints with a larger repair budget; a failed assert halts)."
            ),
        ),
    ] = None,
    device_serial: Annotated[
        str | None,
        typer.Option(
            "--device-serial",
            "-s",
            help="Target specific Android device by serial number (e.g. emulator-5554).",
        ),
    ] = None,
    session_id: Annotated[
        str | None,
        typer.Option(
            "--session-id",
            help="Canonical session UUID for trace and stream telemetry.",
        ),
    ] = None,
    standalone: Annotated[
        bool,
        typer.Option(
            "--standalone",
            help="Run in standalone embedded mode without auto-spawning the Artemis Daemon.",
        ),
    ] = False,
    no_ask_user: Annotated[
        bool,
        typer.Option(
            "--no-ask-user",
            help="Disable ask_user_choice in Flash mode (never pause waiting for user input).",
        ),
    ] = False,
    ask_on_choice: Annotated[
        bool | None,
        typer.Option(
            "--ask-on-choice/--no-ask-on-choice",
            help="Override whether Flash asks the user when multiple comparable options appear.",
        ),
    ] = None,
    ask_when_stuck: Annotated[
        bool | None,
        typer.Option(
            "--ask-when-stuck/--no-ask-when-stuck",
            help="Override whether Flash asks the user when stuck instead of stopping.",
        ),
    ] = None,
) -> None:
    """Run an autonomous UI automation task on the connected Android device."""
    ensure_video_recording_available(with_video_recording_tools)

    console = Console()

    is_worker = (
        os.environ.get("ARTEMIS_TASK_WORKER") == "1"
        or os.environ.get("ARTEMIS_DEVICE_QUEUE_TICKET") is not None
    )
    is_standalone = standalone or os.environ.get("ARTEMIS_STANDALONE") == "1"

    # All platforms route through unified Artemis Daemon unless specifically configured as standalone
    if not is_worker and not is_standalone:
        try:
            import uuid
            from artemis.runtime import (
                ensure_daemon_running,
                get_daemon_pending_choice,
                get_daemon_session,
                get_daemon_status,
                submit_daemon_choice,
                submit_task_to_daemon,
                stop_task_on_daemon,
            )
            from artemis.agents.flash.user_choice import (
                format_pending_choice_prompt,
                parse_choice_input,
            )

            console.print("[dim]Connecting to Artemis Daemon scheduler...[/dim]")
            is_running, base_url = ensure_daemon_running(timeout=8.0, wait_ready=True)
            if is_running and base_url:
                target_sid = session_id or str(uuid.uuid4())
                console.print(
                    f"[bold green]✓[/bold green] Artemis Daemon active at [cyan]{base_url}[/cyan]"
                )
                daemon_ask_on_choice, daemon_ask_when_stuck = _resolve_daemon_user_choice_flags(
                    no_ask_user=no_ask_user,
                    ask_on_choice=ask_on_choice,
                    ask_when_stuck=ask_when_stuck,
                    stdin_isatty=sys.stdin.isatty(),
                )
                resp = submit_task_to_daemon(
                    goal=goal,
                    profile=profile or "pro",
                    device_serial=device_serial,
                    expected_output=output_description,
                    enable_outputter=enable_outputter,
                    locked_app_package=locked_app_package,
                    app_path=app_path,
                    session_id=target_sid,
                    ingress="cli",
                    ask_on_choice=daemon_ask_on_choice,
                    ask_when_stuck=daemon_ask_when_stuck,
                    base_url=base_url,
                )
                if resp and resp.get("tasks"):
                    console.print(
                        f"[bold green]✓[/bold green] Task scheduled in unified queue (Session: [cyan]{target_sid}[/cyan])"
                    )
                    console.print(f"[dim]Live dashboard & replay: {base_url}[/dim]\n")

                    def on_status(sess_info):
                        st = sess_info.get("status")
                        if st == "queued":
                            console.print(
                                "[yellow]⏳ Task queued in scheduler, waiting for device...[/yellow]"
                            )
                        elif st == "running":
                            console.print("[green]▶ Task executing on mobile device...[/green]")

                    try:
                        final_res = _wait_daemon_with_interactive_choices(
                            target_sid,
                            base_url=base_url,
                            console=console,
                            interactive_tty=(not no_ask_user and sys.stdin.isatty()),
                            get_session_fn=get_daemon_session,
                            get_status_fn=get_daemon_status,
                            get_pending_fn=get_daemon_pending_choice,
                            submit_choice_fn=submit_daemon_choice,
                            format_prompt_fn=format_pending_choice_prompt,
                            parse_input_fn=parse_choice_input,
                            on_status_update=on_status,
                        )
                        final_st = final_res.get("status")
                        if final_st in ("completed", "success"):
                            console.print(
                                "\n[bold green]✅ Task completed successfully![/bold green]"
                            )
                            return
                        else:
                            err = final_res.get("error") or final_res.get("explanation") or ""
                            console.print(f"\n[bold red]✖ Task {final_st}[/bold red]: {err}")
                            raise SystemExit(1)
                    except KeyboardInterrupt:
                        console.print("\n[yellow]Stopping task on Daemon...[/yellow]")
                        stop_task_on_daemon(target_sid, base_url=base_url)
                        console.print("[yellow]Task cancelled.[/yellow]")
                        raise SystemExit(130)
                else:
                    console.print(
                        "[yellow]Warning: Could not enqueue task to Daemon. Falling back to local execution...[/yellow]"
                    )
            else:
                console.print(
                    "[yellow]Warning: Artemis Daemon could not be started. Falling back to local execution...[/yellow]"
                )
        except SystemExit:
            raise
        except Exception as exc:
            logger.debug(f"Daemon dispatch error: {exc}")
            console.print(
                f"[yellow]Daemon routing notice: {exc}. Falling back to local execution...[/yellow]"
            )

    display_local_device_status(console, host=settings.ADB_HOST, port=settings.ADB_PORT)

    cancelled = False
    original_sigterm = None
    try:
        if hasattr(signal, "SIGTERM"):
            original_sigterm = signal.signal(
                signal.SIGTERM, lambda s, f: (_ for _ in ()).throw(KeyboardInterrupt())
            )
    except (ValueError, OSError, RuntimeError):
        # Not the main thread or platform does not support SIGTERM handlers;
        # run proceeds without the graceful-cancel hook.
        pass

    try:
        effective_no_ask = no_ask_user or (not is_worker and not sys.stdin.isatty())
        asyncio.run(
            execute_task(
                goal=goal,
                device_serial=device_serial,
                session_id=session_id,
                locked_app_package=locked_app_package,
                test_name=test_name,
                traces_output_path_str=traces_path,
                output_description=output_description,
                video_recording_tools_enabled=with_video_recording_tools,
                profile=profile,
                app_path=app_path,
                enable_planner_validation=enable_planner_validation,
                enable_committee=enable_committee,
                enable_checker=enable_checker,
                enable_step_summarizer=enable_step_summarizer,
                enable_outputter=enable_outputter,
                force_output_synthesis=force_output_synthesis,
                explorer_version=explorer_version,
                explorer_flash_mode=explorer_flash_mode,
                explorer_pro_mode=explorer_pro_mode,
                verification_level=verification_level,
                no_ask_user=effective_no_ask,
                ask_on_choice=ask_on_choice,
                ask_when_stuck=ask_when_stuck,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        # KeyboardInterrupt: Ctrl+C / SIGTERM. CancelledError: the daemon asked
        # this worker to stop through a cancel marker and the Agent cancelled
        # its own task so the recording and trace could be finalized.
        cancelled = True
    except Exception as e:
        err_msg = str(e)
        if "API_KEY" in err_msg or "requires" in err_msg:
            console.print()
            console.print(
                Panel(
                    f"[bold red]✖ Authentication Error:[/bold red] {err_msg}\n\n"
                    "💡 [bold cyan]Quick Fix:[/bold cyan] Run [bold green]artemis init[/bold green] to configure your API key in 10 seconds,\n"
                    "or add [bold]GEMINI_API_KEY=your_key[/bold] to [dim].env[/dim].",
                    title="Missing API Key",
                    expand=False,
                )
            )
            console.print()
            raise SystemExit(1)
        else:
            console.print(f"[bold red]Task execution failed:[/bold red] {e}")
            raise
    finally:
        try:
            if original_sigterm is not None and hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, original_sigterm)
        except (ValueError, OSError, RuntimeError):
            # Best-effort restore of the previous handler on the way out.
            pass
        if cancelled:
            raise SystemExit(130)
