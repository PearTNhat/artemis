"""Unit tests for GeminiKeyPool automatic key rotation and fault tolerance."""

import pytest
from pydantic import SecretStr
from unittest.mock import MagicMock

from artemis.llm.google.key_pool import GeminiKeyPool
from artemis.services.llm import RobustChatModelWrapper, _run_with_recovery
from langchain_google_genai import ChatGoogleGenerativeAI


def test_gemini_key_pool_rotation_and_permanent_lock():
    pool = GeminiKeyPool()
    pool._keys = ["key_A", "key_B", "key_C"]
    pool._initialized = True
    pool._disabled_keys.clear()
    pool._permanent_bad_keys.clear()

    # Initial state
    assert pool.has_multiple_keys()
    first_key = pool.get_active_key()
    assert first_key in ["key_A", "key_B", "key_C"]

    # Test daily quota limit (permanent lock)
    next_key = pool.rotate_key(
        current_key="key_A",
        reason="429 GenerateRequestsPerDayPerProjectPerModel-FreeTier limit: 500",
        permanent=False,  # auto-detected by reason
    )
    assert "key_A" in pool._permanent_bad_keys
    assert next_key in ["key_B", "key_C"]

    # Test temporary RPM limit (60s cooldown)
    next_key_2 = pool.rotate_key(
        current_key="key_B",
        reason="429 Rate limit RPM exceeded",
        permanent=False,
    )
    assert "key_B" in pool._disabled_keys
    assert next_key_2 == "key_C"

    # Daily quota limit on key_B and key_C as well
    pool.rotate_key(current_key="key_B", reason="429 per day limit", permanent=True)
    pool.rotate_key(current_key="key_C", reason="429 daily quota", permanent=True)
    assert pool.get_active_key() is None


def test_update_model_key():
    pool = GeminiKeyPool()
    model = ChatGoogleGenerativeAI(model="gemini-3.5-flash-lite", api_key="initial_key")
    wrapper = RobustChatModelWrapper(model)
    bound = wrapper.bind_tools([])

    # Verify model unwrap and key update
    success = pool.update_model_key(bound, "new_hot_key")
    assert success
    gm = pool.find_google_model(bound)
    assert gm is not None
    assert gm.google_api_key.get_secret_value() == "new_hot_key"


@pytest.mark.asyncio
async def test_run_with_recovery_auto_key_switch():
    pool = GeminiKeyPool()
    pool._keys = ["failing_key", "working_key"]
    pool._initialized = True
    pool._disabled_keys.clear()
    pool._permanent_bad_keys.clear()

    model = ChatGoogleGenerativeAI(model="gemini-3.5-flash-lite", api_key="failing_key")
    wrapper = RobustChatModelWrapper(model)

    call_count = 0

    async def mock_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        gm = pool.find_google_model(wrapper)
        cur_k = gm.google_api_key.get_secret_value()
        if cur_k == "failing_key":
            raise RuntimeError("429 Resource Exhausted: requests per day limit exceeded on Free Tier")
        return "success_response"

    from unittest.mock import patch
    with patch("artemis.llm.google.key_pool.get_key_pool", return_value=pool):
        result = await _run_with_recovery(
            mock_call,
            provider="google",
            endpoint_key="google:gemini-3.5-flash-lite",
            model_wrapper=wrapper,
        )

    assert result == "success_response"
    assert call_count == 2
    assert "failing_key" in pool._permanent_bad_keys
    gm = pool.find_google_model(wrapper)
    assert gm.google_api_key.get_secret_value() == "working_key"


@pytest.mark.asyncio
async def test_single_key_daily_quota_fails_fast_and_locks_key_for_session(monkeypatch, tmp_path):
    from artemis.llm.reliability import LLMPermanentError
    from artemis.services import llm as llm_service

    monkeypatch.setattr(llm_service, "PAUSE_FILE", tmp_path / ".artemis_paused")
    monkeypatch.setattr(llm_service.settings, "LLM_PAUSE_TIMEOUT_SECONDS", 1.0, raising=False)
    llm_service._ENDPOINT_BREAKER._states.clear()

    pool = GeminiKeyPool()
    pool._keys = ["only_key"]
    pool._initialized = True
    pool._disabled_keys.clear()
    pool._permanent_bad_keys.clear()

    model = ChatGoogleGenerativeAI(model="gemini-3.5-flash-lite", api_key="only_key")
    wrapper = RobustChatModelWrapper(model)

    call_count = 0

    async def daily_quota_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError(
            "429 RESOURCE_EXHAUSTED: requests per day limit exceeded on Free Tier"
        )

    from unittest.mock import patch

    with patch("artemis.llm.google.key_pool.get_key_pool", return_value=pool):
        # 1. First failure: must fail fast with LLMPermanentError (no retries, no pause)
        with pytest.raises(LLMPermanentError):
            await _run_with_recovery(
                daily_quota_call,
                provider="google",
                endpoint_key="google:gemini-3.5-flash-lite",
                model_wrapper=wrapper,
            )
        assert call_count == 1
        assert not llm_service.PAUSE_FILE.exists()
        assert pool.is_permanently_blocked("only_key")
        assert pool.all_keys_permanently_blocked()
        assert pool.get_active_key() is None

        # 2. Subsequent call in the same session must reject BEFORE invoking the API again
        with pytest.raises(LLMPermanentError):
            await _run_with_recovery(
                daily_quota_call,
                provider="google",
                endpoint_key="google:gemini-3.5-flash-lite",
                model_wrapper=wrapper,
            )
        assert call_count == 1


@pytest.mark.asyncio
async def test_model_factory_does_not_bypass_locked_pool_with_settings_key(monkeypatch, tmp_path):
    from artemis.llm.reliability import LLMPermanentError
    from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider
    from artemis.services import llm as llm_service

    monkeypatch.setattr(llm_service, "PAUSE_FILE", tmp_path / ".artemis_paused")
    monkeypatch.setattr(
        llm_service.settings, "GOOGLE_API_KEY", SecretStr("only_key"), raising=False
    )

    pool = GeminiKeyPool()
    pool._keys = ["only_key"]
    pool._initialized = True
    pool._disabled_keys.clear()
    pool._permanent_bad_keys = {"only_key"}

    from unittest.mock import patch

    ep = ModelEndpoint(provider=ModelProvider.GOOGLE, model_name="gemini-3.5-flash-lite")
    with patch("artemis.llm.google.key_pool.get_key_pool", return_value=pool):
        raw_model = ModelFactory.create_model(ep)
        wrapper = RobustChatModelWrapper(raw_model, endpoint=ep)
        called = False

        async def should_not_run(*args, **kwargs):
            nonlocal called
            called = True
            return "ok"

        with pytest.raises(LLMPermanentError):
            await _run_with_recovery(
                should_not_run,
                provider="google",
                endpoint_key="google:gemini-3.5-flash-lite",
                model_wrapper=wrapper,
            )
        assert not called
