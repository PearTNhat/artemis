"""The Gemini CLI as a drop-in for an API key on helper calls (one `agy -p` per call).

A fake `agy` script stands in for the CLI: it records its arguments and prints
the JSON envelope `agy -p --output-format json` prints.
"""

import base64
import json
import os
import stat
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from artemis.llm.agy_oneshot import AgyOneShotChatModel, _last_json_object, render_prompt

FAKE_AGY = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_AGY_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
schema = args[args.index("--json-schema") + 1] if "--json-schema" in args else None
out = {"status": "SUCCESS", "response": "plain answer", "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}}
if schema:
    out["structured_output"] = {"package_name": "com.shopee.vn", "found": True}
    out["response"] = json.dumps(out["structured_output"])
print(json.dumps(out))
'''


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    script = tmp_path / "agy"
    script.write_text(FAKE_AGY)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_AGY_LOG", str(log))
    return str(script), log


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


class _Pkg(BaseModel):
    package_name: str
    found: bool


@pytest.mark.asyncio
async def test_plain_call_runs_agy_print_mode_read_only(fake_agy):
    binary, log = fake_agy
    model = AgyOneShotChatModel(model_name="gemini-3.8-flash-low", agy_bin=binary)
    reply = await model.ainvoke([SystemMessage(content="sys"), HumanMessage(content="hi")])
    assert reply.content == "plain answer"
    assert reply.usage_metadata["total_tokens"] == 10
    (args,) = _calls(log)
    assert args[args.index("--mode") + 1] == "plan"
    assert args[args.index("--model") + 1] == "gemini-3.8-flash-low"
    assert args[args.index("--output-format") + 1] == "json"


@pytest.mark.asyncio
async def test_structured_output_uses_agy_json_schema(fake_agy):
    binary, log = fake_agy
    model = AgyOneShotChatModel(model_name="m", agy_bin=binary).with_structured_output(_Pkg)
    out = await model.ainvoke([HumanMessage(content="which package is Shopee?")])
    assert out == _Pkg(package_name="com.shopee.vn", found=True)
    (args,) = _calls(log)
    assert json.loads(args[args.index("--json-schema") + 1])["title"] == "_Pkg"


def test_images_become_files_named_in_the_prompt(tmp_path):
    png = base64.b64encode(b"\x89PNG fake").decode()
    prompt = render_prompt(
        [HumanMessage(content=[
            {"type": "text", "text": "compare"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}},
            {"type": "image", "source_type": "base64", "data": png, "mime_type": "image/jpeg"},
        ])],
        str(tmp_path),
    )
    assert (tmp_path / "image_1.png").read_bytes() == b"\x89PNG fake"
    assert (tmp_path / "image_2.jpg").exists()
    assert str(tmp_path / "image_1.png") in prompt and "2 image file(s)" in prompt


@pytest.mark.asyncio
async def test_temporary_image_directory_is_removed(fake_agy):
    binary, log = fake_agy
    png = base64.b64encode(b"x").decode()
    await AgyOneShotChatModel(agy_bin=binary).ainvoke(
        [HumanMessage(content=[{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}}])]
    )
    (args,) = _calls(log)
    assert not os.path.exists(args[args.index("--add-dir") + 1])


def test_last_json_object_tolerates_log_lines_before_the_envelope():
    assert _last_json_object('warming up...\n{"status": "SUCCESS", "response": "ok"}') == {
        "status": "SUCCESS",
        "response": "ok",
    }


def test_model_spec_routes_agy_prefix_to_the_cli_and_plain_names_to_the_api():
    from artemis.services.llm import get_model_by_spec

    assert isinstance(get_model_by_spec("agy:gemini-3.8-flash-low"), AgyOneShotChatModel)
    assert get_model_by_spec("agy:gemini-3.8-flash-low").model_name == "gemini-3.8-flash-low"
    assert not isinstance(get_model_by_spec("gemini-3.5-flash-lite"), AgyOneShotChatModel)


def test_conversation_model_answers_structured_requests_one_shot():
    """Checker / Hopper on provider "agy": structured output must use --json-schema."""
    from artemis.llm.agy_model import AgyChatModel

    runnable = AgyChatModel(model_name="gemini-3.8-flash-low").with_structured_output(_Pkg)
    assert runnable is not None
    assert "AgyOneShot" in repr(runnable) or callable(getattr(runnable, "ainvoke", None))


def test_provider_name_maps_to_the_one_shot_model():
    from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider

    assert ModelProvider.from_string("agy_oneshot") is ModelProvider.AGY_ONESHOT
    model = ModelFactory.create_model(
        ModelEndpoint(provider=ModelProvider.AGY_ONESHOT, model_name="gemini-3.8-flash-low")
    )
    assert isinstance(model, AgyOneShotChatModel)
