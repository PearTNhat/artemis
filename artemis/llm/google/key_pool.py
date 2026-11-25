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

"""Thread-safe Gemini API Key Pool with automated key rotation and cooldown management.

Enables seamless real-time key switching on HTTP 429 (Rate Limit) or HTTP 403 (Invalid/Unbilled)
during actual device UI automation and testing runs, eliminating interruptions and pauses.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any

from artemis.llm.reliability import is_daily_quota_message

logger = logging.getLogger("artemis.llm.google.key_pool")


class GeminiKeyPool:
    """Manages active Gemini API keys, health tracking, and hot-swapping."""

    COOLDOWN_SECONDS: float = 60.0

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._keys: list[str] = []
        self._disabled_keys: dict[str, float] = {}  # key -> timestamp when disabled
        self._permanent_bad_keys: set[str] = set()
        self._current_index: int = 0
        self._initialized: bool = False
        self._ensure_initialized()

    @property
    def keys(self) -> list[str]:
        self._ensure_initialized()
        with self._lock:
            return list(self._keys)

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return

            # 1. Check settings first if available
            raw = ""
            try:
                from artemis.config import settings
                if hasattr(settings, "GEMINI_API_KEYS") and settings.GEMINI_API_KEYS:
                    raw = settings.GEMINI_API_KEYS.get_secret_value()
                elif hasattr(settings, "GEMINI_API_KEY") and settings.GEMINI_API_KEY:
                    raw = settings.GEMINI_API_KEY.get_secret_value()
            except Exception:
                pass

            # 2. Check environment variables
            if not raw:
                raw = os.environ.get("GEMINI_API_KEYS", os.environ.get("GEMINI_API_KEY", ""))

            # 3. Read directly from .env files as fallback
            if not raw:
                try:
                    from artemis.config.paths import get_env_file
                    env_file = get_env_file()
                    if env_file.exists():
                        for line in env_file.read_text(encoding="utf-8").splitlines():
                            line = line.strip()
                            if line.startswith("GEMINI_API_KEYS="):
                                raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                                break
                            elif line.startswith("GEMINI_API_KEY=") and not raw:
                                raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                except Exception:
                    pass

            if not raw:
                repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
                for p in [os.path.join(repo_root, ".env"), os.path.join(repo_root, "artemis", ".env")]:
                    if os.path.exists(p):
                        try:
                            for line in open(p, encoding="utf-8"):
                                line = line.strip()
                                if line.startswith("GEMINI_API_KEYS="):
                                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                                    break
                                elif line.startswith("GEMINI_API_KEY=") and not raw:
                                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                        except Exception:
                            pass
                        if raw:
                            break

            # Parse and deduplicate preserving order
            keys: list[str] = []
            seen: set[str] = set()
            for part in raw.split(","):
                k = part.strip()
                if not k or "your_" in k.lower() or "example" in k.lower():
                    continue
                if k not in seen:
                    seen.add(k)
                    keys.append(k)

            self._keys = keys
            self._current_index = random.randint(0, max(0, len(keys) - 1)) if keys else 0
            self._initialized = True

    def get_keys(self) -> list[str]:
        self._ensure_initialized()
        with self._lock:
            return list(self._keys)

    def has_keys(self) -> bool:
        self._ensure_initialized()
        with self._lock:
            return bool(self._keys)

    def has_multiple_keys(self) -> bool:
        self._ensure_initialized()
        with self._lock:
            return len(self._keys) > 1

    def is_permanently_blocked(self, key: str | None) -> bool:
        """True if ``key`` was locked for this session (daily quota / auth failure)."""
        if not key:
            return False
        self._ensure_initialized()
        with self._lock:
            return key in self._permanent_bad_keys

    def all_keys_permanently_blocked(self) -> bool:
        """True when keys are configured and every one of them is session-locked."""
        self._ensure_initialized()
        with self._lock:
            return bool(self._keys) and all(
                k in self._permanent_bad_keys for k in self._keys
            )

    @staticmethod
    def mask_key(key: str | None) -> str:
        if not key:
            return "None"
        return f"...{key[-6:]}" if len(key) >= 6 else key

    def get_active_key(self) -> str | None:
        """Returns the currently selected active key, clearing expired cooldowns."""
        self._ensure_initialized()
        with self._lock:
            now = time.time()
            # Restore keys whose cooldown has expired
            recovered = []
            for k, disabled_at in list(self._disabled_keys.items()):
                if now - disabled_at >= self.COOLDOWN_SECONDS:
                    recovered.append(k)
                    del self._disabled_keys[k]
            if recovered:
                logger.info(
                    "🟢 [Gemini Key Pool] Hạn mức 60s đã phục hồi cho %d key(s).",
                    len(recovered),
                )

            active = [
                k for k in self._keys
                if k not in self._permanent_bad_keys and k not in self._disabled_keys
            ]
            if not active:
                # If ALL keys were temporarily limited, clear cooldown to allow retry
                if self._disabled_keys:
                    logger.warning(
                        "⚠️ [Gemini Key Pool] Toàn bộ %d Gemini keys đều bị limit, reset tạm khóa.",
                        len(self._keys),
                    )
                    self._disabled_keys.clear()
                    active = [k for k in self._keys if k not in self._permanent_bad_keys]
                if not active:
                    return None

            return active[self._current_index % len(active)]

    def get_next_key(self) -> str | None:
        """Advances to and returns the next active key (Round-Robin)."""
        self._ensure_initialized()
        with self._lock:
            key = self.get_active_key()
            self._current_index += 1
            return key

    def rotate_key(
        self,
        current_key: str | None = None,
        reason: str = "Rate Limit 429",
        permanent: bool = False,
    ) -> str | None:
        """Marks current_key as failed (temp or permanent) and hot-swaps to the next available key."""
        self._ensure_initialized()
        if is_daily_quota_message(reason):
            permanent = True

        with self._lock:
            now = time.time()
            target_key = current_key
            if not target_key:
                active = [
                    k for k in self._keys
                    if k not in self._permanent_bad_keys and k not in self._disabled_keys
                ]
                if active:
                    target_key = active[self._current_index % len(active)]

            if target_key:
                if permanent:
                    self._permanent_bad_keys.add(target_key)
                    self._disabled_keys.pop(target_key, None)
                    status_text = "Đã khóa vĩnh viễn trong phiên"
                else:
                    self._disabled_keys[target_key] = now
                    status_text = f"Đã tạm khóa {self.COOLDOWN_SECONDS:.0f}s"

                remaining = [
                    k for k in self._keys
                    if k not in self._permanent_bad_keys and k not in self._disabled_keys
                ]
                logger.warning(
                    "🚫 [Gemini Key %s] %s! %s (còn lại %d/%d keys khả dụng).",
                    self.mask_key(target_key),
                    reason,
                    status_text,
                    len(remaining),
                    len(self._keys),
                )

            # Look for next active key
            active = [
                k for k in self._keys
                if k not in self._permanent_bad_keys and k not in self._disabled_keys
            ]
            if not active:
                if self._disabled_keys:
                    logger.warning(
                        "⚠️ [Gemini Key Pool] Toàn bộ %d key đều đang trong thời gian chờ hồi hạn mức.",
                        len(self._keys),
                    )
                return None

            self._current_index += 1
            next_key = active[self._current_index % len(active)]
            logger.info(
                "🔄 [Gemini Key Pool] Đã tự động chuyển sang key %s.",
                self.mask_key(next_key),
            )
            return next_key

    def find_google_model(self, model: Any) -> Any | None:
        """Unwraps any layer of LangChain bindings to find the underlying ChatGoogleGenerativeAI."""
        from langchain_google_genai import ChatGoogleGenerativeAI

        curr = model
        while curr is not None:
            if isinstance(curr, ChatGoogleGenerativeAI):
                return curr
            curr = getattr(curr, "bound", None) or getattr(curr, "base_model", None)
        return None

    def update_model_key(self, model: Any, new_key: str) -> bool:
        """Hot-swaps the underlying Google GenAI client and secret key on the active model."""
        gm = self.find_google_model(model)
        if gm is None:
            return False

        from google import genai
        from pydantic import SecretStr

        try:
            gm.google_api_key = SecretStr(new_key)
            client_args = getattr(gm, "client_args", {}) or {}
            gm.client = genai.Client(api_key=new_key, **client_args)
            return True
        except Exception as err:
            logger.error("Failed to hot-swap Gemini client key: %s", err)
            return False


_GLOBAL_KEY_POOL: GeminiKeyPool | None = None
_INIT_LOCK = threading.Lock()


def get_key_pool() -> GeminiKeyPool:
    """Singleton provider for the global GeminiKeyPool."""
    global _GLOBAL_KEY_POOL
    if _GLOBAL_KEY_POOL is None:
        with _INIT_LOCK:
            if _GLOBAL_KEY_POOL is None:
                _GLOBAL_KEY_POOL = GeminiKeyPool()
    return _GLOBAL_KEY_POOL

