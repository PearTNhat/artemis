"""Debug logger for Android Output, AI Input, and AI Output.

Provides a unified, switchable mechanism to inspect:
1. [ANDROID OUTPUT]: Screen resolution, screenshot metadata, scrollable state, and indexed UI elements list.
2. [AI INPUT]: Formatted prompt messages, active observation tail, available tools (safely truncating large base64 images).
3. [AI OUTPUT]: Model reasoning/thinking, structured tool calls with resolved parameters, and token usage.

Can be enabled via:
- Environment variable: `ARTEMIS_DEBUG_AI_IO=true` (or `1`, `yes`, `on`) in `.env`
- Or fallback: `ARTEMIS_DEBUG=true`
- Or programmatically: `set_ai_io_debug_enabled(True)`
"""

import datetime
import json
import logging
import os
import re
import sys
from typing import Any

from colorama import Fore, Style

logger = logging.getLogger("artemis.ai_io")

# Global override if set programmatically
_OVERRIDE_ENABLED: bool | None = None

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

def get_ai_io_log_file_path() -> str:
    """Returns the absolute path to the dedicated ai_io.log file."""
    # Try Workspace relative path first, fallback to home
    workspace_log = "/home/nhat/Workspace/control-android/artemis/logs/ai_io.log"
    return workspace_log

def _write_to_log_file(text: str) -> None:
    """Appends clean text without ANSI colors to the persistent log file."""
    try:
        log_path = get_ai_io_log_file_path()
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        clean_text = _ANSI_ESCAPE_RE.sub("", text)
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[{now_str}]\n{clean_text}\n")
    except Exception as exc:
        logger.debug(f"Could not append to {log_path}: {exc}")


def is_ai_io_debug_enabled() -> bool:
    """Checks whether AI I/O debug logging is enabled."""
    global _OVERRIDE_ENABLED
    if _OVERRIDE_ENABLED is not None:
        return _OVERRIDE_ENABLED

    val = os.environ.get("ARTEMIS_DEBUG_AI_IO", "").strip().lower()
    if val in ("true", "1", "yes", "on", "y"):
        return True
    if val in ("false", "0", "no", "off", "n"):
        return False

    # Fallback to general ARTEMIS_DEBUG
    debug_val = os.environ.get("ARTEMIS_DEBUG", "").strip().lower()
    return debug_val in ("true", "1", "yes", "on", "y")


def set_ai_io_debug_enabled(enabled: bool | None) -> None:
    """Programmatically override the AI I/O debug switch."""
    global _OVERRIDE_ENABLED
    _OVERRIDE_ENABLED = enabled


def _truncate_base64_in_content(content: Any) -> Any:
    """Replaces huge base64 strings with human-readable summary."""
    if isinstance(content, str):
        if len(content) > 500 and "data:image/" in content and "base64," in content:
            prefix = content[: content.find("base64,") + 7]
            b64_len = len(content) - len(prefix)
            return f"{prefix}<base64_truncated ({b64_len:,} chars, ~{b64_len*3//4//1024} KB)>"
        return content

    if isinstance(content, list):
        sanitized = []
        for item in content:
            if isinstance(item, dict):
                item_copy = dict(item)
                if item_copy.get("type") == "image_url":
                    img_url = item_copy.get("image_url", {})
                    if isinstance(img_url, dict) and "url" in img_url:
                        raw_url = img_url["url"]
                        if len(raw_url) > 200:
                            item_copy["image_url"] = {
                                "url": f"<JPEG image base64 ({len(raw_url):,} chars, ~{len(raw_url)*3//4//1024} KB)>"
                            }
                elif item_copy.get("type") == "image" and isinstance(item_copy.get("base64"), str):
                    raw = item_copy["base64"]
                    item_copy["base64"] = f"<JPEG image base64 ({len(raw):,} chars, ~{len(raw)*3//4//1024} KB)>"
                elif "text" in item_copy and isinstance(item_copy["text"], str):
                    item_copy["text"] = _truncate_base64_in_content(item_copy["text"])
                sanitized.append(item_copy)
            else:
                sanitized.append(item)
        return sanitized

    return content


def log_android_output(
    width: int | None = None,
    height: int | None = None,
    screenshot_bytes: bytes | None = None,
    xml_list: str | None = None,
    scrollable: bool | None = None,
    elements_count: int | None = None,
    turn: int | None = None,
) -> None:
    """Logs the observation data returned from the Android device."""
    if not is_ai_io_debug_enabled():
        return

    turn_str = f" | Turn {turn}" if turn is not None else ""
    sep = "=" * 80
    header = f"{Fore.CYAN}{Style.BRIGHT}{sep}\n📱 [1] OUTPUT TỪ ANDROID (Screen & Observation){turn_str}\n{'-'*80}{Style.RESET_ALL}"

    shot_size_kb = (len(screenshot_bytes) / 1024.0) if screenshot_bytes else 0.0
    dim_str = f"{width}x{height}" if width and height else "Unknown"
    scroll_str = (
        "Có (scrollable)"
        if scrollable is True
        else ("Không (fixed)" if scrollable is False else "Chưa rõ")
    )

    meta_info = (
        f"{Fore.CYAN}• Màn hình:{Style.RESET_ALL} {dim_str} | "
        f"{Fore.CYAN}Ảnh chụp:{Style.RESET_ALL} {shot_size_kb:.1f} KB ({len(screenshot_bytes) if screenshot_bytes else 0:,} bytes) | "
        f"{Fore.CYAN}Cuộn:{Style.RESET_ALL} {scroll_str}"
    )
    if elements_count is not None:
        meta_info += f" | {Fore.CYAN}Số elements:{Style.RESET_ALL} {elements_count}"

    ui_header = f"{Fore.CYAN}• Danh sách UI Elements (Indexed cho AI):{Style.RESET_ALL}"
    ui_body = xml_list.strip() if xml_list else "(Không có phần tử UI nào được định vị)"

    output = f"\n{header}\n{meta_info}\n{ui_header}\n{ui_body}\n{Fore.CYAN}{sep}{Style.RESET_ALL}\n"
    print(output, flush=True)
    _write_to_log_file(output)


def log_ai_input(
    messages: list[Any],
    tools: list[Any] | None = None,
    turn: int | None = None,
) -> None:
    """Logs the exact prompt payload and tools sent into the AI."""
    if not is_ai_io_debug_enabled():
        return

    turn_str = f" | Turn {turn}" if turn is not None else ""
    sep = "=" * 80
    header = f"{Fore.YELLOW}{Style.BRIGHT}{sep}\n🧠 [2] INPUT ĐƯA VÀO AI (Prompt & Messages gửi đến LLM){turn_str}\n{'-'*80}{Style.RESET_ALL}"

    tool_names = []
    if tools:
        for t in tools:
            if hasattr(t, "name"):
                tool_names.append(t.name)
            elif isinstance(t, dict) and "name" in t:
                tool_names.append(t["name"])
            elif isinstance(t, dict) and "function" in t and "name" in t["function"]:
                tool_names.append(t["function"]["name"])
            elif isinstance(t, str):
                tool_names.append(t)

    tools_str = (
        f"{Fore.YELLOW}• Tools khả dụng ({len(tool_names)}):{Style.RESET_ALL} "
        f"{', '.join(tool_names) if tool_names else 'None'}"
    )
    msg_summary = f"{Fore.YELLOW}• Tổng số Messages gửi đi:{Style.RESET_ALL} {len(messages)}"

    lines = [f"\n{header}", tools_str, msg_summary, f"{Fore.YELLOW}{'-'*80}{Style.RESET_ALL}"]

    for idx, msg in enumerate(messages, 1):
        role = getattr(msg, "type", msg.__class__.__name__)
        content = getattr(msg, "content", "")
        safe_content = _truncate_base64_in_content(content)

        tool_calls = getattr(msg, "tool_calls", None)
        tc_str = ""
        if tool_calls:
            tc_str = f"\n  Tool Calls: {json.dumps(tool_calls, ensure_ascii=False)}"

        if isinstance(safe_content, list):
            formatted_blocks = []
            for b in safe_content:
                if isinstance(b, dict):
                    b_type = b.get("type", "")
                    if b_type == "text":
                        formatted_blocks.append(b.get("text", ""))
                    elif b_type == "image_url":
                        url_str = b.get("image_url", {}).get("url", "")
                        formatted_blocks.append(f"[Screenshot: {url_str}]")
                    elif b_type == "image":
                        formatted_blocks.append(
                            f"[Screenshot: {b.get('base64', '')} media_resolution={b.get('media_resolution', '-')}]"
                        )
                    else:
                        formatted_blocks.append(json.dumps(b, ensure_ascii=False))
                else:
                    formatted_blocks.append(str(b))
            body = "\n".join(formatted_blocks)
        else:
            body = str(safe_content)

        lines.append(f"{Fore.YELLOW}[Message {idx} | {role}]:{Style.RESET_ALL}")
        lines.append(body.strip())
        if tc_str:
            lines.append(tc_str)
        lines.append(f"{Fore.YELLOW}{'-'*40}{Style.RESET_ALL}")

    lines.append(f"{Fore.YELLOW}{sep}{Style.RESET_ALL}\n")
    final_text = "\n".join(lines)
    print(final_text, flush=True)
    _write_to_log_file(final_text)


def log_ai_output(
    response: Any,
    latency_s: float | None = None,
    turn: int | None = None,
) -> None:
    """Logs the raw response, reasoning thoughts, and tool decisions returned by the AI."""
    if not is_ai_io_debug_enabled():
        return

    turn_str = f" | Turn {turn}" if turn is not None else ""
    lat_str = f" (thời gian: {latency_s:.2f}s)" if latency_s is not None else ""
    sep = "=" * 80
    header = f"{Fore.GREEN}{Style.BRIGHT}{sep}\n🤖 [3] OUTPUT AI TRẢ VỀ (Phản hồi & Tool Calls từ LLM){turn_str}{lat_str}\n{'-'*80}{Style.RESET_ALL}"

    content = getattr(response, "content", "")
    thinking = ""
    raw_content = ""

    if isinstance(content, str):
        raw_content = content
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "thinking":
                    thinking += item.get("thinking", "")
                elif item.get("type") == "text":
                    raw_content += item.get("text", "")
            elif isinstance(item, str):
                raw_content += item

    additional = getattr(response, "additional_kwargs", {}) or {}
    if not thinking and isinstance(additional, dict):
        thinking = additional.get("thinking", "")

    tool_calls = getattr(response, "tool_calls", []) or []

    lines = [f"\n{header}"]

    if thinking:
        lines.append(f"{Fore.GREEN}• Thinking (Deep Reasoning):{Style.RESET_ALL}")
        lines.append(thinking.strip())
        lines.append(f"{Fore.GREEN}{'-'*40}{Style.RESET_ALL}")

    if raw_content:
        lines.append(f"{Fore.GREEN}• Suy luận / Phản hồi (Content):{Style.RESET_ALL}")
        lines.append(raw_content.strip())
        lines.append(f"{Fore.GREEN}{'-'*40}{Style.RESET_ALL}")

    lines.append(f"{Fore.GREEN}• Tool Calls AI quyết định gọi ({len(tool_calls)}):{Style.RESET_ALL}")
    if tool_calls:
        for idx, tc in enumerate(tool_calls, 1):
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "unknown")
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
            lines.append(f"  {Fore.GREEN}[{idx}]{Style.RESET_ALL} {Style.BRIGHT}{name}{Style.RESET_ALL}({args_str})")
    else:
        lines.append("  (Không có tool call nào — AI chỉ trả lời bằng text)")

    usage = getattr(response, "usage_metadata", None) or getattr(response, "response_metadata", {}).get("token_usage")
    if usage:
        lines.append(f"{Fore.GREEN}• Token Usage:{Style.RESET_ALL} {usage}")

    lines.append(f"{Fore.GREEN}{sep}{Style.RESET_ALL}\n")
    final_text = "\n".join(lines)
    print(final_text, flush=True)
    _write_to_log_file(final_text)

