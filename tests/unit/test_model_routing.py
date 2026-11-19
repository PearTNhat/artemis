"""The UI labels a setup "Gemini CLI" only when every required node runs on the CLI."""

from types import SimpleNamespace
from unittest.mock import patch

from artemis.config.model_routing import describe_model_routing


def _llm(helper_provider: str):
    cli = SimpleNamespace(provider="agy", model="gemini-3.8-flash-low")
    helper = SimpleNamespace(provider=helper_provider, model="gemini-3.5-flash-lite")
    er = SimpleNamespace(provider="google", model="gemini-robotics-er-2-preview")
    agents = {"summarizer": helper, "validator_pixel_safety_net": helper, "planner_validation": helper}
    utils = {"hopper": helper, "object_detector": er, "video_analyzer": er}
    return SimpleNamespace(
        get_agent=lambda n: agents.get(n, cli),
        get_utils=lambda n: utils.get(n, cli),
    )


def _agent(step_model: str, explorer: str):
    return SimpleNamespace(
        flash=SimpleNamespace(
            explorer_mode=explorer, step_summarizer=SimpleNamespace(model=step_model, enabled=True)
        ),
        pro=SimpleNamespace(explorer=SimpleNamespace(mode=explorer)),
        memory=SimpleNamespace(chunking=SimpleNamespace(model=step_model)),
    )


def _describe(helper_provider, step_model, explorer):
    with (
        patch("artemis.config.parse_llm_config", return_value=_llm(helper_provider)),
        patch("artemis.config.load_agent_config", return_value=_agent(step_model, explorer)),
    ):
        return describe_model_routing()


def test_agent_on_cli_but_helpers_on_api_key_is_not_cli_only():
    """The setup that ran out of free API quota while labelled "Gemini CLI"."""
    out = _describe("google", "gemini-3.5-flash-lite", "flash")
    assert out["cli_only"] is False
    assert set(out["api_key_nodes"]) >= {
        "hopper", "summarizer", "validator_pixel_safety_net", "planner_validation",
        "flash_step_summarizer", "history_compression",
    }
    assert "object_detector" in out["api_key_on_demand"]


def test_every_required_node_on_the_cli_is_cli_only():
    out = _describe("agy_oneshot", "agy:gemini-3.8-flash-low", "flash")
    assert out["cli_only"] is True
    assert out["api_key_nodes"] == []
    # ER grounding is on demand and API-only: listed, not counted against "CLI only".
    er = next(n for n in out["nodes"] if n["node"] == "object_detector")
    assert er["required"] is False and not er["uses_cli"]
    assert set(out["api_key_on_demand"]) == {"object_detector", "video_analyzer"}


def test_judge_nodes_in_config_override_their_default():
    """config comment says to add "planner_validation": {...} under nodes; it was ignored."""
    from artemis.config.llm import _expand_default_into_nodes

    out = _expand_default_into_nodes(
        {
            "default": {"provider": "agy", "model": "gemini-3.8-flash-low"},
            "nodes": {"validator_pixel_safety_net": {"provider": "agy_oneshot", "model": "m"}},
        }
    )
    assert out["validator_pixel_safety_net"] == {"provider": "agy_oneshot", "model": "m"}
    assert "planner_validation" not in out  # absent -> lightweight_judge_default
