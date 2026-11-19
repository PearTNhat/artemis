"""One-shot chat model on the local Antigravity CLI (``agy -p``), for helper calls.

``AgyChatModel`` keeps one long ``agy`` conversation per task: right for the
agents that reason turn after turn (Flash, Operator, Planner, Checker). Helper
models are different: many small, independent, often parallel calls (a step
summary, a history capsule, an app-package lookup), each a full request of its
own, exactly like an API call. Routing them through one long conversation would
either serialize them or restart the conversation on every call.

This model makes each call a separate ``agy -p`` run, so a Gemini CLI
subscription can replace a Gemini API key for every node:

* images in the messages are written to a private temporary directory that is
  added to agy's workspace (``--add-dir``) and named in the prompt;
* ``with_structured_output(schema)`` passes ``--json-schema`` and validates the
  ``structured_output`` agy returns;
* calls run in parallel up to ``MAX_CONCURRENT_CALLS`` processes;
* agy runs in ``--mode plan`` (read-only): it cannot run commands or edit files.

A call costs one agy process start (measured ~6 s for a short text answer,
~10 s with an image), several times an API round trip. Use it for background
helpers and rare blocking lookups, not for the per-turn agent loop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import tempfile
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

#: agy processes allowed at once across every one-shot model of the process.
MAX_CONCURRENT_CALLS = 4
_SEMAPHORE: asyncio.Semaphore | None = None
_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None

_MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/webp": ".webp"}


class AgyOneShotError(RuntimeError):
    """An ``agy -p`` call failed (process error, timeout, non-success status)."""


def _semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE, _SEMAPHORE_LOOP
    loop = asyncio.get_running_loop()
    if _SEMAPHORE is None or _SEMAPHORE_LOOP is not loop:
        _SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
        _SEMAPHORE_LOOP = loop
    return _SEMAPHORE


def _image_payload(block: dict) -> tuple[bytes, str] | None:
    """``(bytes, extension)`` of an image content block, in any LangChain shape."""
    kind = block.get("type")
    data: str | None = None
    mime = "image/png"
    if kind == "image_url":
        url = block.get("image_url")
        url = url.get("url") if isinstance(url, dict) else url
        if isinstance(url, str) and url.startswith("data:"):
            header, _, data = url.partition(",")
            mime = header[5:].split(";")[0] or mime
    elif kind == "image":
        data = block.get("data") or block.get("base64")
        mime = block.get("mime_type") or block.get("mimeType") or mime
        source = block.get("source")
        if not data and isinstance(source, dict):
            data = source.get("data")
            mime = source.get("media_type") or mime
    if not data:
        return None
    try:
        return base64.b64decode(data), _MIME_EXT.get(mime, ".png")
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def render_prompt(messages: list[BaseMessage], image_dir: str) -> str:
    """One prompt text for agy; images are saved under ``image_dir`` and named."""
    parts: list[str] = []
    image_count = 0
    for message in messages:
        if isinstance(message, SystemMessage):
            header = "# INSTRUCTIONS"
        elif isinstance(message, AIMessage):
            header = "# ASSISTANT (earlier reply)"
        else:
            header = "# INPUT"
        chunks: list[str] = []
        content = message.content
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text", "")))
            elif isinstance(block, dict):
                image = _image_payload(block)
                if image is None:
                    continue
                image_count += 1
                path = os.path.join(image_dir, f"image_{image_count}{image[1]}")
                with open(path, "wb") as fh:
                    fh.write(image[0])
                chunks.append(f"[Image {image_count}: open and look at the file {path}]")
        parts.append(header + "\n" + "\n".join(c for c in chunks if c))
    if image_count:
        parts.append(
            f"There are {image_count} image file(s) referenced above; read each one before answering."
        )
    parts.append("Answer the request above directly. Do not run commands or edit files.")
    return "\n\n".join(parts)


class AgyOneShotChatModel(BaseChatModel):
    """Each call is one independent ``agy -p`` run (see the module docstring)."""

    model_name: str | None = None
    agy_bin: str = "agy"
    timeout_s: float = 120.0
    json_schema: dict | None = None

    @property
    def _llm_type(self) -> str:
        return "agy-oneshot"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}

    @property
    def model(self) -> str | None:  # read by callers that log the model in use
        return self.model_name

    async def _run(self, messages: list[BaseMessage], json_schema: dict | None) -> dict:
        binary = shutil.which(self.agy_bin) or self.agy_bin
        workdir = tempfile.mkdtemp(prefix="artemis-agy-oneshot-")
        try:
            prompt = render_prompt(messages, workdir)
            cmd = [
                binary, "-p", prompt,
                "--mode", "plan",
                "--add-dir", workdir,
                "--output-format", "json",
                "--print-timeout", f"{int(self.timeout_s)}s",
            ]
            if self.model_name:
                cmd += ["--model", self.model_name]
            if json_schema:
                cmd += ["--json-schema", json.dumps(json_schema)]
            async with _semaphore():
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=workdir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=self.timeout_s + 15
                    )
                except asyncio.TimeoutError as err:
                    proc.kill()
                    await proc.wait()
                    raise AgyOneShotError(f"agy -p timed out after {self.timeout_s}s") from err
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        text = stdout.decode("utf-8", errors="replace").strip()
        payload = _last_json_object(text)
        if proc.returncode != 0 or payload is None:
            detail = (stderr.decode("utf-8", errors="replace") or text)[-500:]
            raise AgyOneShotError(f"agy -p failed (exit {proc.returncode}): {detail}")
        if str(payload.get("status", "SUCCESS")).upper() != "SUCCESS":
            raise AgyOneShotError(f"agy -p returned status {payload.get('status')}: {text[-500:]}")
        return payload

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        payload = await self._run(messages, self.json_schema)
        content = payload.get("response") or ""
        if self.json_schema and payload.get("structured_output") is not None:
            content = json.dumps(payload["structured_output"], ensure_ascii=False)
        usage = payload.get("usage") or {}
        message = AIMessage(
            content=str(content).strip(),
            response_metadata={"model_name": self.model_name, "provider": "agy_oneshot"},
            usage_metadata={
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return asyncio.run(self._agenerate(messages, stop=stop, **kwargs))

    def with_structured_output(self, schema: Any, **kwargs: Any):  # type: ignore[override]
        """Structured answers through agy's own ``--json-schema`` enforcement."""
        is_model = isinstance(schema, type) and issubclass(schema, BaseModel)
        json_schema = schema.model_json_schema() if is_model else dict(schema)
        bound = self.model_copy(update={"json_schema": json_schema})

        async def _call(messages: Any) -> Any:
            if isinstance(messages, str):
                from langchain_core.messages import HumanMessage

                messages = [HumanMessage(content=messages)]
            payload = await bound._run(list(messages), json_schema)
            data = payload.get("structured_output")
            if data is None:
                data = _last_json_object(str(payload.get("response") or ""))
            return schema.model_validate(data) if is_model else data

        return RunnableLambda(lambda m: asyncio.run(_call(m)), afunc=_call)


def _last_json_object(text: str) -> dict | None:
    """The JSON object printed by ``agy -p --output-format json`` (last one wins)."""
    text = text.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                continue
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None
    return None
