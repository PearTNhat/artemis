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

"""System Readiness & Diagnostics Router for Artemis Admin Console."""

import ipaddress
import os
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from artemis.core.diagnostics import readiness_engine
from artemis.core.diagnostics.adb_server_connection import (
    InvalidAdbServerEndpoint,
    adb_server_connection,
)
from artemis.core.diagnostics.schema import SystemReadinessReport

router = APIRouter(prefix="/api/system", tags=["system"])


def _require_local_admin_request(request: Request) -> None:
    """Keep endpoint probing and mutation on the local administration boundary."""
    client_host = request.client.host if request.client else None
    allow_remote = os.getenv("ARTEMIS_ALLOW_REMOTE_ADB_CONFIGURATION", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if client_host and not allow_remote:
        try:
            is_loopback = ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            is_loopback = client_host.lower() == "localhost"
        if not is_loopback:
            raise HTTPException(
                status_code=403,
                detail=(
                    "ADB server settings are local-only. Set "
                    "ARTEMIS_ALLOW_REMOTE_ADB_CONFIGURATION=true to manage them from another "
                    "computer."
                ),
            )

    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin or not host:
        return
    origin_host = urlsplit(origin).netloc.lower()
    if origin_host != host.lower():
        raise HTTPException(
            status_code=403,
            detail="ADB server settings can only be changed from the Artemis console.",
        )


def _require_loopback_request(request: Request, detail: str) -> None:
    """Reject requests whose TCP peer is not the local machine."""
    client_host = request.client.host if request.client else None
    try:
        is_loopback = bool(client_host and ipaddress.ip_address(client_host).is_loopback)
    except ValueError:
        is_loopback = bool(client_host and client_host.lower() == "localhost")
    if not is_loopback:
        raise HTTPException(status_code=403, detail=detail)


def _require_local_lifecycle_request(request: Request) -> None:
    """Authorize a process-lifecycle request from the local CLI only."""
    _require_loopback_request(request, "Server lifecycle controls are local-only.")

    expected = getattr(request.app.state, "lifecycle_token", None)
    supplied = request.headers.get("x-artemis-lifecycle-token")
    if not (
        isinstance(expected, str)
        and isinstance(supplied, str)
        and secrets.compare_digest(expected, supplied)
    ):
        raise HTTPException(status_code=403, detail="Invalid server lifecycle token.")


class SelectDeviceRequest(BaseModel):
    """Payload to select an active target Android device."""

    serial: str = Field(description="Serial number or identifier of the Android device to select")


@router.get("/readiness", response_model=SystemReadinessReport)
async def get_system_readiness(force: bool = False) -> SystemReadinessReport:
    """Execute all diagnostic probes and return a comprehensive system readiness report."""
    return await readiness_engine.run_all(force_refresh=force)


@router.post("/devices/select")
async def select_active_device(request: SelectDeviceRequest):
    """Select the active Android device or emulator for subsequent automated tasks."""
    serial = request.serial.strip()
    if not serial:
        raise HTTPException(status_code=400, detail="Device serial cannot be empty.")

    readiness_engine.set_probe_target_serial(serial)
    # Return updated readiness
    report = await readiness_engine.run_all(force_refresh=True)
    return {
        "status": "success",
        "selected_serial": serial,
        "report": report,
    }


@router.post("/adb/restart")
async def restart_adb_server():
    """Restart local ADB server and return an updated readiness check."""
    restart_result = await readiness_engine.restart_adb_server()
    readiness_engine.invalidate_cache()
    updated_report = await readiness_engine.run_all(force_refresh=True)
    return {
        "restart_result": restart_result,
        "report": updated_report,
    }


@router.post("/adb/heal-keys")
async def heal_adb_keys():
    """Auto-heal corrupted ADB authentication RSA keys and return updated readiness."""
    heal_result = await readiness_engine.heal_adb_keys()
    updated_report = await readiness_engine.run_all()
    return {
        "heal_result": heal_result,
        "report": updated_report,
    }


class ConnectAdbRequest(BaseModel):
    """Payload to connect to an Android device over Wi-Fi."""

    host: str = Field(description="IP address of Android device")
    port: int = Field(default=5555, description="Port number")


@router.post("/adb/connect")
async def connect_wireless_adb(request: ConnectAdbRequest):
    """Connect to a device over Wi-Fi and return updated readiness."""
    connect_result = await readiness_engine.connect_wireless_adb(request.host, request.port)
    readiness_engine.invalidate_cache()
    updated_report = await readiness_engine.run_all(force_refresh=True)
    return {
        "connect_result": connect_result,
        "report": updated_report,
    }


class ConnectAdbServerRequest(BaseModel):
    """Payload to select an ADB server endpoint accessible from this computer."""

    host: str = Field(description="Host name or IP address of the ADB server")
    port: int = Field(default=5037, ge=1, le=65535, description="ADB server port")
    persist: bool = Field(default=True, description="Persist the endpoint for future launches")


@router.get("/adb/server")
async def get_adb_server_status():
    """Return the process-wide ADB server endpoint currently used by Artemis."""
    return adb_server_connection.status()


@router.post("/adb/server/connect")
async def connect_adb_server(payload: ConnectAdbServerRequest, request: Request):
    """Validate and activate an ADB server endpoint."""
    _require_local_admin_request(request)
    try:
        connection_result = await adb_server_connection.connect(
            payload.host,
            payload.port,
            persist=payload.persist,
        )
    except InvalidAdbServerEndpoint as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response: dict[str, object] = {"connection_result": connection_result}
    if connection_result["success"]:
        readiness_engine.set_probe_target_serial(None)
        readiness_engine.invalidate_cache()
        response["report"] = await readiness_engine.run_all(force_refresh=True)
    return response


@router.post("/adb/server/probe")
async def probe_adb_server(payload: ConnectAdbServerRequest, request: Request):
    """Test an ADB server endpoint without changing the active endpoint."""
    _require_local_admin_request(request)
    try:
        connection_result = await adb_server_connection.probe(payload.host, payload.port)
    except InvalidAdbServerEndpoint as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"connection_result": connection_result}


@router.post("/adb/server/local")
async def use_local_adb_server(request: Request, persist: bool = True):
    """Restore the standard local ADB server without touching a remote daemon."""
    _require_local_admin_request(request)
    connection_result = await adb_server_connection.use_local_server(persist=persist)
    readiness_engine.set_probe_target_serial(None)
    readiness_engine.invalidate_cache()
    updated_report = await readiness_engine.run_all(force_refresh=True)
    return {
        "connection_result": connection_result,
        "report": updated_report,
    }


class LaunchEmulatorRequest(BaseModel):
    """Payload to launch a local Android Virtual Device (AVD)."""

    avd_name: str = Field(
        description="Name of the installed AVD emulator to launch (e.g. Android_2)"
    )


@router.post("/emulator/launch")
async def launch_emulator(request: LaunchEmulatorRequest):
    """Launch an Android emulator in the background and return initiation status."""
    avd_name = request.avd_name.strip()
    if not avd_name:
        raise HTTPException(status_code=400, detail="AVD name cannot be empty.")

    launch_res = await readiness_engine.launch_emulator(avd_name)
    return launch_res


@router.get("/emulator/status")
async def get_emulator_status():
    """Query real-time progress and logs of background emulator launch."""
    return readiness_engine.get_emulator_status()


@router.post("/emulator/stop")
async def stop_emulator():
    """Stop active emulator process."""
    return await readiness_engine.stop_emulator()


@router.post("/emulator/dismiss")
async def dismiss_emulator():
    """Dismiss emulator launch tracking state."""
    return readiness_engine.dismiss_emulator()


class UpdateCredentialsRequest(BaseModel):
    """Payload to update and configure LLM or Vision OCR API credentials."""

    provider: str = Field(
        default="google",
        description="Provider identifier (e.g. google, gemini, openai, anthropic, openrouter, ocr)",
    )
    api_key: str = Field(description="The secret API key string to configure")
    persist_to_env: bool = Field(
        default=True, description="Whether to persist the key to .env file"
    )


class ValidateCredentialsRequest(BaseModel):
    """Payload to test and verify LLM or Vision OCR API credentials without saving."""

    provider: str = Field(
        default="google",
        description="Provider identifier (e.g. google, gemini, openai, anthropic, openrouter, ocr)",
    )
    api_key: str = Field(description="The secret API key string to test")
    base_url: str | None = Field(default=None, description="Optional custom base URL")


class SwitchPresetRequest(BaseModel):
    """Payload to switch active configuration preset."""

    preset: str = Field(description="Preset name: fosta, qwen, gemini")


class UpdateCustomConfigRequest(BaseModel):
    """Payload to dynamically update model, api_key, and base_url."""

    model: str = Field(description="Model identifier, e.g. gpt-6.1-sol")
    api_key: str | None = Field(default=None, description="API Key")
    base_url: str | None = Field(default=None, description="Base URL")


@router.get("/credentials")
async def get_credentials():
    """Report which providers have an API key configured.

    Secret values never leave the process: this endpoint intentionally returns
    presence booleans only. Keys are written via POST /credentials and used
    server-side.
    """
    from artemis.config import settings

    providers = ("google", "openai", "anthropic", "openrouter", "ocr")
    status = {name: bool(settings.get_api_key(name)) for name in providers}
    status["gemini"] = status["google"]
    return {
        "providers": [
            {"name": name, "configured": configured} for name, configured in status.items()
        ]
    }


@router.post("/credentials/test")
async def test_credentials(request: ValidateCredentialsRequest):
    """Test and verify whether an API key is valid and usable with the corresponding provider endpoint."""
    from artemis.utils.credentials_validator import validate_api_key

    provider = request.provider.strip().lower()
    key = request.api_key.strip()

    if not key:
        raise HTTPException(status_code=400, detail="API key cannot be empty.")

    is_valid, message = await validate_api_key(
        provider=provider,
        api_key=key,
        base_url=request.base_url,
    )
    if not is_valid:
        raise HTTPException(status_code=400, detail=message)

    return {
        "valid": True,
        "provider": provider,
        "message": message,
    }


@router.post("/credentials")
async def update_credentials(request: UpdateCredentialsRequest):
    """Dynamically configure and persist LLM or Vision API key, returning updated readiness report."""
    from artemis.utils.credentials_validator import validate_api_key

    provider = request.provider.strip().lower()
    key = request.api_key.strip()

    # If a non-empty key is provided, verify it before saving
    if key:
        is_valid, validation_msg = await validate_api_key(provider=provider, api_key=key)
        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail=f"API key verification failed: {validation_msg}",
            )

    try:
        from artemis.config import settings

        settings.set_api_key(provider, key, persist_to_env=request.persist_to_env)

        # Re-run all diagnostic probes to build updated report
        readiness_engine.invalidate_cache()
        updated_report = await readiness_engine.run_all(force_refresh=True)
        action_desc = (
            "successfully verified, updated, and applied" if key else "successfully cleared"
        )
        return {
            "status": "success",
            "message": f"API key for {provider} {action_desc}.",
            "provider": provider,
            "report": updated_report,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to update credentials: {exc}")


class DebugAiIoRequest(BaseModel):
    enabled: bool


@router.get("/debug-ai-io")
async def get_debug_ai_io():
    """Returns the current state of ARTEMIS_DEBUG_AI_IO."""
    from artemis.utils.ai_io_logger import is_ai_io_debug_enabled

    return {"enabled": is_ai_io_debug_enabled()}


@router.post("/debug-ai-io")
async def set_debug_ai_io(payload: DebugAiIoRequest):
    """Dynamically toggle ARTEMIS_DEBUG_AI_IO at runtime and persist to .env."""
    import logging
    import os
    from artemis.config.paths import get_env_file
    from artemis.utils.ai_io_logger import set_ai_io_debug_enabled

    app_logger = logging.getLogger("artemis.system")
    enabled = payload.enabled
    set_ai_io_debug_enabled(enabled)
    os.environ["ARTEMIS_DEBUG_AI_IO"] = "true" if enabled else "false"

    # Persist to .env
    try:
        env_file = get_env_file()
        if env_file.exists():
            lines = env_file.read_text(encoding="utf-8").splitlines()
            new_lines = []
            found = False
            for line in lines:
                if (
                    line.startswith("ARTEMIS_DEBUG_AI_IO=")
                    or line.startswith("#ARTEMIS_DEBUG_AI_IO=")
                    or line.startswith("# ARTEMIS_DEBUG_AI_IO=")
                ):
                    new_lines.append(f"ARTEMIS_DEBUG_AI_IO={'true' if enabled else 'false'}")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"ARTEMIS_DEBUG_AI_IO={'true' if enabled else 'false'}")
            env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception as e:
        app_logger.warning(f"Could not persist ARTEMIS_DEBUG_AI_IO to .env: {e}")

    return {"status": "success", "enabled": enabled}


class RecordingToggleRequest(BaseModel):
    enabled: bool


def _is_video_recording_active() -> bool:
    """Helper to detect if screen recording is currently active."""
    v = os.getenv("ARTEMIS_VIDEO_RECORDING_ENABLED")
    if v is not None:
        v_clean = v.strip().lower()
        if v_clean in ("1", "true", "yes", "on"):
            return True
        if v_clean in ("0", "false", "no", "off"):
            return False
    from artemis.config.agent import load_agent_config

    try:
        cfg = load_agent_config()
        if cfg.video_analyzer and cfg.video_analyzer.enabled is not None:
            return bool(cfg.video_analyzer.enabled)
    except Exception:
        pass
    from artemis.utils.video import detect_video_tools_enabled

    return detect_video_tools_enabled()


@router.get("/recording")
async def get_recording_status():
    """Returns whether automated video recording via scrcpy is enabled."""
    return {"enabled": _is_video_recording_active()}


@router.post("/recording")
async def set_recording_status(payload: RecordingToggleRequest):
    """Dynamically toggle automated video recording and persist to .env."""
    import logging
    import os
    from artemis.config.paths import get_env_file

    app_logger = logging.getLogger("artemis.system")
    enabled = payload.enabled
    os.environ["ARTEMIS_VIDEO_RECORDING_ENABLED"] = "true" if enabled else "false"

    # Persist to .env
    try:
        env_file = get_env_file()
        if env_file.exists():
            lines = env_file.read_text(encoding="utf-8").splitlines()
            new_lines = []
            found = False
            for line in lines:
                if (
                    line.startswith("ARTEMIS_VIDEO_RECORDING_ENABLED=")
                    or line.startswith("#ARTEMIS_VIDEO_RECORDING_ENABLED=")
                    or line.startswith("# ARTEMIS_VIDEO_RECORDING_ENABLED=")
                ):
                    new_lines.append(
                        f"ARTEMIS_VIDEO_RECORDING_ENABLED={'true' if enabled else 'false'}"
                    )
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(
                    f"ARTEMIS_VIDEO_RECORDING_ENABLED={'true' if enabled else 'false'}"
                )
            env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception as e:
        app_logger.warning(f"Could not persist ARTEMIS_VIDEO_RECORDING_ENABLED to .env: {e}")

    return {"status": "success", "enabled": enabled}


@router.get("/model-config-env")
async def get_model_config_and_env():
    """Retrieve the current active artemis.jsonc configuration and .env status for custom setup."""
    import os
    from artemis.config.paths import get_config_path, get_env_file
    from artemis.config import settings
    from third_party.mobile_use.utils.file import load_jsonc

    from artemis.config.settings import is_placeholder_key

    # 1. Config file resolution
    config_path = None
    config_content = ""
    parsed_config = {}
    try:
        config_path_obj = get_config_path("artemis.jsonc")
        config_path = str(config_path_obj)
        config_content = config_path_obj.read_text(encoding="utf-8")
        with open(config_path_obj, encoding="utf-8") as f:
            parsed_config = load_jsonc(f)
    except Exception as e:
        config_content = f"// Error reading config: {e}"

    # 2. Env file resolution
    env_path = get_env_file()

    # 3. Relevant Env Variables status
    def read_raw_env_file_val(key_name: str) -> str | None:
        try:
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    clean = line.strip()
                    if clean.startswith(f"{key_name}="):
                        val = clean.split("=", 1)[1].strip()
                        return val if val and not is_placeholder_key(val) else None
        except Exception:
            pass
        return None

    def get_real_env_value(key_name: str, provider: str) -> str | None:
        raw = read_raw_env_file_val(key_name)
        if raw:
            return raw
        k = settings.get_api_key(provider)
        if k and not is_placeholder_key(k):
            return k.get_secret_value()
        val = os.environ.get(key_name)
        if val and val.strip() and not is_placeholder_key(val):
            return val.strip()
        return None

    def mask_key(k: str | None) -> str | None:
        # Expose only enough to recognize which key is active, never a usable
        # fragment of the secret itself.
        if not k:
            return None
        if len(k) <= 8:
            return "****"
        return f"****{k[-4:]}"

    gemini_real = get_real_env_value("GEMINI_API_KEY", "google") or get_real_env_value(
        "GOOGLE_API_KEY", "google"
    )
    openai_real = get_real_env_value("OPENAI_API_KEY", "openai")
    anthropic_real = get_real_env_value("ANTHROPIC_API_KEY", "anthropic")
    openrouter_real = get_real_env_value("OPEN_ROUTER_API_KEY", "openrouter")
    xai_real = get_real_env_value("XAI_API_KEY", "xai")
    base_url_val = read_raw_env_file_val("OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or settings.OPENAI_BASE_URL
    if base_url_val and is_placeholder_key(base_url_val):
        base_url_val = None
    ocr_real = get_real_env_value("OCR_API_KEY", "ocr") or get_real_env_value(
        "VISION_API_KEY", "ocr"
    )

    env_vars = [
        {
            "name": "GEMINI_API_KEY",
            "provider": "google",
            "is_set": bool(gemini_real),
            "preview": mask_key(gemini_real),
            "description": "Google Gemini multimodal vision API key (free tier available)",
        },
        {
            "name": "OPENAI_API_KEY",
            "provider": "openai",
            "is_set": bool(openai_real),
            "preview": mask_key(openai_real),
            "description": "OpenAI API key (GPT-4o, GPT-4o-mini)",
        },
        {
            "name": "ANTHROPIC_API_KEY",
            "provider": "anthropic",
            "is_set": bool(anthropic_real),
            "preview": mask_key(anthropic_real),
            "description": "Anthropic Claude API key (Claude 3.5 Sonnet, Claude 3.7)",
        },
        {
            "name": "OPEN_ROUTER_API_KEY",
            "provider": "openrouter",
            "is_set": bool(openrouter_real),
            "preview": mask_key(openrouter_real),
            "description": "OpenRouter unified API gateway key",
        },
        {
            "name": "XAI_API_KEY",
            "provider": "xai",
            "is_set": bool(xai_real),
            "preview": mask_key(xai_real),
            "description": "xAI Grok vision API key",
        },
        {
            "name": "OPENAI_BASE_URL",
            "provider": "custom",
            "is_set": bool(base_url_val),
            "preview": base_url_val,
            "description": "Custom API endpoint (for local Ollama, vLLM, DeepSeek, or proxies)",
        },
        {
            "name": "VISION_API_KEY",
            "provider": "ocr",
            "is_set": bool(ocr_real),
            "preview": mask_key(ocr_real),
            "description": "Google Cloud Vision OCR key for screen text detection (optional)",
        },
    ]

    # Per-node backend (Gemini CLI vs API key) so the UI labels the setup by
    # what really runs, not just by the default model's provider.
    try:
        from artemis.config.model_routing import describe_model_routing

        model_routing = describe_model_routing()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        import logging

        logging.getLogger(__name__).warning("Could not describe model routing: %s", exc)
        model_routing = None

    return {
        "model_routing": model_routing,
        "config_path": config_path or "config/artemis.jsonc",
        "config_filename": "artemis.jsonc",
        "config_content": config_content,
        "default_model": parsed_config.get("default", {}),
        "presets": parsed_config.get("presets", {}),
        "env_path": str(env_path),
        "env_filename": ".env",
        "env_vars": env_vars,
    }


@router.post("/model-config/switch-preset")
async def switch_model_preset_endpoint(req: SwitchPresetRequest):
    """Switch active configuration preset between available presets (fosta, qwen, gemini, agy, etc.)."""
    preset = req.preset.strip().lower()
    from artemis.config.paths import get_config_path, get_env_file
    from artemis.config import settings
    import shutil, re, os

    cfg_dir = get_config_path("artemis.jsonc").parent
    preset_file = cfg_dir / f"artemis.{preset}.jsonc"
    target_file = cfg_dir / "artemis.jsonc"

    if not preset_file.exists():
        raise HTTPException(status_code=400, detail=f"Preset '{preset}' not found.")

    def set_env_kv(content: str, key: str, value: str) -> str:
        pattern = rf"^{key}=.*"
        if re.search(pattern, content, flags=re.MULTILINE):
            return re.sub(pattern, f"{key}={value}", content, flags=re.MULTILINE)
        return content.rstrip() + f"\n{key}={value}\n"

    env_file = get_env_file()
    if env_file.exists():
        content = env_file.read_text(encoding="utf-8")
        
        # Dynamically resolve credentials from environment or .env without hardcoding secrets
        preset_key_var = f"{preset.upper()}_API_KEY"
        preset_url_var = f"{preset.upper()}_BASE_URL"

        preset_api_key = os.getenv(preset_key_var)
        preset_base_url = os.getenv(preset_url_var)

        for line in content.splitlines():
            clean = line.strip()
            if clean.startswith(f"{preset_key_var}=") and not preset_api_key:
                preset_api_key = clean.split("=", 1)[1].strip().strip('"').strip("'")
            elif clean.startswith(f"{preset_url_var}=") and not preset_base_url:
                preset_base_url = clean.split("=", 1)[1].strip().strip('"').strip("'")

        if preset_api_key:
            content = set_env_kv(content, "OPENAI_API_KEY", preset_api_key)
            os.environ["OPENAI_API_KEY"] = preset_api_key
        if preset_base_url:
            content = set_env_kv(content, "OPENAI_BASE_URL", preset_base_url)
            os.environ["OPENAI_BASE_URL"] = preset_base_url
            if hasattr(settings, "OPENAI_BASE_URL"):
                settings.OPENAI_BASE_URL = preset_base_url
        env_file.write_text(content, encoding="utf-8")

    shutil.copy2(preset_file, target_file)
    readiness_engine.invalidate_cache()
    return {"status": "success", "preset": preset, "message": f"Switched to {preset} successfully."}


@router.post("/model-config/update-custom")
async def update_custom_config_endpoint(req: UpdateCustomConfigRequest):
    """Dynamically update model name in artemis.jsonc and API key / base URL in .env."""
    from artemis.config.paths import get_config_path, get_env_file
    from artemis.config import settings
    import re, os

    target_file = get_config_path("artemis.jsonc")
    if target_file.exists():
        content = target_file.read_text(encoding="utf-8")
        # Safely update ONLY the default model inside "default": { ... }
        # Preserving comments and critical specialized nodes such as "object_detector"
        model_pattern = r'("default"\s*:\s*\{[^{}]*?"model"\s*:\s*)"[^"]*"'
        if re.search(model_pattern, content, flags=re.DOTALL):
            content = re.sub(model_pattern, rf'\g<1>"{req.model}"', content, count=1, flags=re.DOTALL)

        # If a custom base_url is specified, ensure default provider is set to "openai"
        if req.base_url and req.base_url.strip():
            provider_pattern = r'("default"\s*:\s*\{[^{}]*?"provider"\s*:\s*)"[^"]*"'
            if re.search(provider_pattern, content, flags=re.DOTALL):
                content = re.sub(provider_pattern, r'\g<1>"openai"', content, count=1, flags=re.DOTALL)

        target_file.write_text(content, encoding="utf-8")

    def set_env_kv(content: str, key: str, value: str) -> str:
        pattern = rf"^{key}=.*"
        if re.search(pattern, content, flags=re.MULTILINE):
            return re.sub(pattern, f"{key}={value}", content, flags=re.MULTILINE)
        return content.rstrip() + f"\n{key}={value}\n"

    env_file = get_env_file()
    if env_file.exists():
        env_content = env_file.read_text(encoding="utf-8")
        if req.api_key is not None and req.api_key.strip():
            k = req.api_key.strip()
            os.environ["OPENAI_API_KEY"] = k
            env_content = set_env_kv(env_content, "OPENAI_API_KEY", k)
        if req.base_url is not None and req.base_url.strip():
            u = req.base_url.strip()
            os.environ["OPENAI_BASE_URL"] = u
            if hasattr(settings, "OPENAI_BASE_URL"):
                settings.OPENAI_BASE_URL = u
            env_content = set_env_kv(env_content, "OPENAI_BASE_URL", u)
        env_file.write_text(env_content, encoding="utf-8")

    readiness_engine.invalidate_cache()
    return {"status": "success", "message": "Updated custom configuration successfully."}


@router.get("/server-status")
async def get_server_runtime_status():
    """Retrieve runtime status, PID, port, and uptime of the Artemis server.

    Answers from in-process state. ``server_lifecycle.get_server_status`` is
    not used here: it runs ``lsof``/``fuser``, which can take over a second and
    is too slow for the ``is_artemis_daemon`` probe.
    """
    import os
    import time

    from artemis.runtime.process_probe import pid_is_alive
    from artemis.runtime.server_lifecycle import read_server_info

    try:
        from apps.admin_console.core.state import state
    except ImportError:
        from admin_console.core.state import state

    port = getattr(state, "port", 8000)
    current_pid = os.getpid()
    pids = {current_pid}
    started_at = None

    info = read_server_info()
    if info and info.get("port") == port:
        saved_pid = info.get("pid")
        if isinstance(saved_pid, int) and saved_pid != current_pid and pid_is_alive(saved_pid):
            pids.add(saved_pid)
        if isinstance(info.get("started_at"), (int, float)):
            started_at = float(info["started_at"])
    if started_at is None:
        try:
            import psutil

            started_at = psutil.Process(current_pid).create_time()
        except Exception:  # pylint: disable=broad-exception-caught
            # psutil is optional; uptime is best-effort.
            started_at = None

    uptime_seconds = max(0.0, time.time() - started_at) if started_at is not None else None
    # Explicit DTO: the raw metadata file additionally holds the lifecycle
    # token, cmdline, and filesystem paths, none of which belong on the wire.
    return {
        "running": True,
        "port": port,
        "pids": sorted(pids),
        "active_pid": current_pid,
        "uptime_seconds": uptime_seconds,
        "url": f"http://localhost:{port}",
        "admin_url": f"http://localhost:{port}/admin",
        "current_pid": current_pid,
    }


@router.post("/restart")
async def restart_server_endpoint(request: Request):
    """Request a graceful restart of the Artemis server from thin clients/UI."""
    import asyncio
    import os
    import sys
    import threading

    _require_loopback_request(request, "Server lifecycle controls are local-only.")

    try:
        from apps.admin_console.core.state import state
    except ImportError:
        from admin_console.core.state import state

    port = getattr(state, "port", 8000)
    current_pid = os.getpid()

    def _restart_worker():
        import time

        time.sleep(0.6)
        if sys.platform != "win32":
            try:
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception:
                import subprocess

                subprocess.Popen([sys.executable] + sys.argv)
                os._exit(0)
        else:
            import subprocess

            subprocess.Popen([sys.executable] + sys.argv)
            os._exit(0)

    threading.Thread(target=_restart_worker, daemon=True).start()

    return {
        "status": "restarting",
        "message": "Artemis server is restarting. Client reconnection should occur in 2-3 seconds.",
        "previous_pid": current_pid,
        "port": port,
    }


@router.post("/shutdown", status_code=202)
async def shutdown_server_endpoint(request: Request):
    """Request a graceful shutdown of the Artemis server."""
    import asyncio

    try:
        from apps.admin_console.core.state import state
    except ImportError:
        from admin_console.core.state import state

    _require_local_lifecycle_request(request)
    server = getattr(request.app.state, "uvicorn_server", None)
    if server is None:
        raise HTTPException(status_code=503, detail="Server lifecycle controller is unavailable.")

    async def _shutdown_after_response() -> None:
        # Let Starlette flush the accepted response before Uvicorn leaves its
        # main loop and invokes the FastAPI shutdown lifecycle.
        await asyncio.sleep(0.05)
        state.is_shutting_down = True
        state.shutdown_event.set()
        server.should_exit = True

    asyncio.create_task(_shutdown_after_response())

    return {
        "status": "shutting_down",
        "message": "Artemis server is shutting down.",
        "pid": os.getpid(),
    }


class McpInstallRequest(BaseModel):
    client: str = Field(description="Client target: 'claude', 'antigravity', 'cursor', 'all'")


@router.get("/cli-integrations")
async def get_cli_integrations_status():
    """Check availability and MCP connection status of Claude Code CLI and Gemini CLI / Antigravity."""
    import json
    from pathlib import Path
    import shutil

    home = Path.home()

    # 1. Claude CLI / Claude Code
    claude_bin = shutil.which("claude")
    claude_code_config = home / ".claude.json"
    claude_configured = False
    if claude_code_config.exists():
        try:
            data = json.loads(claude_code_config.read_text(encoding="utf-8"))
            claude_configured = "artemis" in data.get("mcpServers", {})
        except Exception:
            pass

    # 2. Gemini CLI / Antigravity (agy)
    agy_bin = shutil.which("agy")
    gemini_mcp_config = home / ".gemini" / "antigravity" / "mcp_config.json"
    alt_gemini_config = home / ".gemini" / "config" / "mcp_config.json"
    gemini_configured = False
    for p in (gemini_mcp_config, alt_gemini_config):
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if "artemis" in data.get("mcpServers", {}):
                    gemini_configured = True
                    break
            except Exception:
                pass

    return {
        "claude": {
            "name": "Claude Code (Claude CLI)",
            "bin": claude_bin,
            "installed": claude_bin is not None,
            "configured": claude_configured,
            "command": "claude",
            "install_cmd": "uv run artemis mcp --install claude",
        },
        "antigravity": {
            "name": "Gemini CLI / Antigravity (agy)",
            "bin": agy_bin,
            "installed": agy_bin is not None,
            "configured": gemini_configured,
            "command": "agy",
            "install_cmd": "uv run artemis mcp --install antigravity",
        },
    }


@router.post("/mcp-install")
async def install_mcp_integration(payload: McpInstallRequest):
    """Auto-install ARTEMIS MCP configuration and testing rules into target CLI/IDE."""
    from pathlib import Path
    import sys

    from artemis.interfaces.cli.commands.mcp import install_mcp_config, install_rules

    client = payload.client.strip().lower()
    project_root = str(Path(__file__).resolve().parents[3])
    venv_py = Path(project_root) / ".venv" / "bin" / "python"
    python_exe = str(venv_py) if venv_py.exists() else sys.executable

    try:
        cfg_paths = install_mcp_config(client, python_exe, project_root)
        rule_paths = install_rules(client, project_root)
        return {
            "status": "success",
            "client": client,
            "config_paths": cfg_paths,
            "rule_paths": rule_paths,
            "message": f"Đã kết nối thành công ARTEMIS MCP với {client}!",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi khi cài đặt MCP: {e}")
