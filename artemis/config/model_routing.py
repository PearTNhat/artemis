"""Which backend each model-using node actually runs on: Gemini CLI or an API key.

The UI used to label the whole setup by the default model's provider, so a
"Gemini CLI" setup looked CLI-only while helpers (app lookup, step summaries,
history compression, the Pro safety net, the Explorer's grounding model) still
called the Gemini API with the free key and stopped when its daily quota ran
out. ``describe_model_routing`` lists every node with its real provider so the
UI can say "CLI only" only when it is true.
"""

from __future__ import annotations

from typing import Any

CLI_PROVIDERS = frozenset({"agy", "agy_oneshot", "antigravity", "geminicli"})

#: Agent nodes resolved through ``LLMConfig.get_agent``.
_AGENT_NODES = (
    "planner",
    "operator",
    "checker",
    "summarizer",
    "explorer",
    "validator_pixel_safety_net",
    "planner_validation",
)
#: Utility nodes resolved through ``LLMConfig.get_utils``.
_UTIL_NODES = ("outputter", "hopper", "video_analyzer", "object_detector")


def _provider_of_spec(spec: str | None) -> tuple[str, str]:
    """``(provider, model)`` of a ``[provider:]model`` string (see get_model_by_spec)."""
    spec = (spec or "").strip()
    head, sep, tail = spec.partition(":")
    if sep and head.strip().lower().replace("-", "_") in ("agy", "agy_oneshot", "gemini_cli"):
        return "agy_oneshot", tail.strip()
    return "google", spec


def _entry(node: str, provider: Any, model: Any, *, required: bool = True, note: str = "") -> dict:
    prov = str(getattr(provider, "value", provider) or "").lower()
    return {
        "node": node,
        "provider": prov,
        "model": str(model or ""),
        "uses_cli": prov.replace("-", "_") in CLI_PROVIDERS,
        "required": required,
        "note": note,
    }


def describe_model_routing() -> dict:
    """Every model-using node with its provider, plus a CLI-only verdict.

    ``required=False`` marks nodes a run only calls on demand (the video
    analyzer, the ER grounding model when no Explorer tier uses it); they are
    listed but do not break "CLI only".
    """
    from artemis.config import load_agent_config, parse_llm_config

    nodes: list[dict] = []
    llm = parse_llm_config()
    for name in _AGENT_NODES:
        try:
            cfg = llm.get_agent(name)
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        nodes.append(_entry(name, cfg.provider, cfg.model))

    agent_cfg = None
    try:
        agent_cfg = load_agent_config()
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    explorer_tiers: set[str] = set()
    if agent_cfg is not None:
        flash_tier = getattr(agent_cfg.flash, "explorer_mode", None)
        pro_tier = getattr(getattr(getattr(agent_cfg, "pro", None), "explorer", None), "mode", None)
        explorer_tiers = {str(getattr(t, "value", t)) for t in (flash_tier, pro_tier) if t}

    for name in _UTIL_NODES:
        try:
            cfg = llm.get_utils(name)
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        if cfg is None:
            continue
        if name == "video_analyzer":
            nodes.append(_entry(name, cfg.provider, cfg.model, required=False,
                                note="only when a run analyzes its screen recording"))
        elif name == "object_detector":
            # Called only when the agent asks the Explorer to locate an element
            # (not every step), and the ER grounding model exists only on the
            # Gemini API: listed, but not counted against "CLI only".
            used = "flash" in explorer_tiers
            nodes.append(_entry(name, cfg.provider, cfg.model, required=False,
                                note="on demand: 'flash' Explorer grounding (ask_explorer)" if used
                                else "unused: no Explorer tier is 'flash'"))
        else:
            nodes.append(_entry(name, cfg.provider, cfg.model))

    if agent_cfg is not None:
        step = getattr(getattr(agent_cfg.flash, "step_summarizer", None), "model", None)
        enabled = getattr(getattr(agent_cfg.flash, "step_summarizer", None), "enabled", True)
        provider, model = _provider_of_spec(step)
        nodes.append(_entry("flash_step_summarizer", provider, model, required=bool(enabled)))
        chunk_model = getattr(getattr(agent_cfg.memory, "chunking", None), "model", None)
        if chunk_model:
            provider, model = _provider_of_spec(chunk_model)
            nodes.append(_entry("history_compression", provider, model))

    api_nodes = [n["node"] for n in nodes if n["required"] and not n["uses_cli"]]
    on_demand_api = [
        n["node"] for n in nodes
        if not n["required"] and not n["uses_cli"] and not n["note"].startswith("unused")
    ]
    default_provider = next((n["provider"] for n in nodes if n["node"] == "operator"), "")
    return {
        "nodes": nodes,
        "api_key_nodes": api_nodes,
        #: Called only on demand (Explorer grounding, video analysis) through an API key.
        "api_key_on_demand": on_demand_api,
        "cli_only": default_provider in CLI_PROVIDERS and not api_nodes,
    }
