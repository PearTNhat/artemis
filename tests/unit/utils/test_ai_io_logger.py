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

"""Tests for ai_io_logger (session-aware debug logging)."""

from pathlib import Path
from types import SimpleNamespace

from artemis.utils import ai_io_logger


def test_is_ai_io_debug_enabled_switch(monkeypatch):
    ai_io_logger.set_ai_io_debug_enabled(None)
    monkeypatch.delenv("ARTEMIS_DEBUG_AI_IO", raising=False)
    monkeypatch.delenv("ARTEMIS_DEBUG", raising=False)
    assert not ai_io_logger.is_ai_io_debug_enabled()

    monkeypatch.setenv("ARTEMIS_DEBUG_AI_IO", "true")
    assert ai_io_logger.is_ai_io_debug_enabled()

    monkeypatch.setenv("ARTEMIS_DEBUG_AI_IO", "false")
    assert not ai_io_logger.is_ai_io_debug_enabled()

    # Programmatic override takes precedence
    ai_io_logger.set_ai_io_debug_enabled(True)
    assert ai_io_logger.is_ai_io_debug_enabled()
    ai_io_logger.set_ai_io_debug_enabled(False)
    assert not ai_io_logger.is_ai_io_debug_enabled()
    ai_io_logger.set_ai_io_debug_enabled(None)


def test_get_ai_io_log_file_path(tmp_path):
    session_dir = tmp_path / "traces" / "sess-123"
    assert ai_io_logger.get_ai_io_log_file_path(session_dir) == session_dir / "ai_io.log"

    default_path = ai_io_logger.get_ai_io_log_file_path()
    assert default_path.name == "ai_io.log"
    assert default_path.parent.name == "logs"


def test_session_aware_logging(tmp_path, monkeypatch):
    ai_io_logger.set_ai_io_debug_enabled(True)
    global_log = tmp_path / "global" / "ai_io.log"
    monkeypatch.setattr(ai_io_logger, "get_ai_io_log_file_path", lambda sdir=None: (Path(sdir) / "ai_io.log") if sdir else global_log)

    sess1_dir = tmp_path / "traces" / "sess-1"
    sess2_dir = tmp_path / "traces" / "sess-2"

    # Start session 1
    ai_io_logger.log_session_start(session_id="sess-1", goal="Goal 1", session_dir=sess1_dir)
    ai_io_logger.log_android_output(
        width=1080, height=2400, xml_list='[1] "OK" 0,0,1,1', turn=1,
        session_id="sess-1", session_dir=sess1_dir
    )
    ai_io_logger.log_ai_input(
        messages=[SimpleNamespace(type="human", content="Hello")],
        turn=1, session_id="sess-1", session_dir=sess1_dir
    )
    ai_io_logger.log_ai_output(
        response=SimpleNamespace(content="Hi there", tool_calls=[]),
        latency_s=1.2, turn=1, session_id="sess-1", session_dir=sess1_dir
    )

    # Session 1 log should exist and have its content
    s1_log = sess1_dir / "ai_io.log"
    assert s1_log.exists()
    s1_text = s1_log.read_text(encoding="utf-8")
    assert "sess-1" in s1_text
    assert "Goal 1" in s1_text
    assert "[1] OUTPUT TỪ ANDROID" in s1_text
    assert "[2] INPUT ĐƯA VÀO AI" in s1_text
    assert "[3] OUTPUT AI TRẢ VỀ" in s1_text
    assert "Hi there" in s1_text

    # Start session 2
    ai_io_logger.log_session_start(session_id="sess-2", goal="Goal 2", session_dir=sess2_dir)
    ai_io_logger.log_ai_output(
        response=SimpleNamespace(content="Response 2", tool_calls=[]),
        latency_s=0.8, turn=1, session_id="sess-2", session_dir=sess2_dir
    )

    s2_log = sess2_dir / "ai_io.log"
    assert s2_log.exists()
    s2_text = s2_log.read_text(encoding="utf-8")
    assert "sess-2" in s2_text
    assert "Goal 2" in s2_text
    assert "Response 2" in s2_text
    # Session 2 must NOT have Session 1's goal or response
    assert "Goal 1" not in s2_text
    assert "Hi there" not in s2_text

    # Global log should have both sessions demarcated
    assert global_log.exists()
    g_text = global_log.read_text(encoding="utf-8")
    assert "BẮT ĐẦU PHIÊN CHAT / TASK: [sess-1]" in g_text
    assert "BẮT ĐẦU PHIÊN CHAT / TASK: [sess-2]" in g_text
    assert "Goal 1" in g_text
    assert "Goal 2" in g_text

    ai_io_logger.set_ai_io_debug_enabled(None)
