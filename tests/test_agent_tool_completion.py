# Copyright (C) 2026 Andrea Marson (am.dev.75@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Reasoning-effort contract for the Gateway agent tool-calling path."""

from types import SimpleNamespace

from retriva.config import settings
from retriva.qa import answerer


class _Completions:
    def __init__(self, captured):
        self.captured = captured

    def create(self, **kwargs):
        self.captured.update(kwargs)
        return "response"


class _Client:
    def __init__(self, captured):
        self.chat = SimpleNamespace(completions=_Completions(captured))


def _capture(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        answerer, "OpenAI", lambda **kwargs: _Client(captured))
    return captured


def _call():
    return answerer.chat_completion_with_tools(
        [{"role": "user", "content": "create the campaign import"}],
        [{"type": "function", "function": {"name": "probe",
                                           "parameters": {"type": "object"}}}],
        "auto",
    )


def test_agent_reasoning_effort_defaults_to_low():
    from retriva.config import Settings

    field = Settings.model_fields["agent_reasoning_effort"]
    assert field.default == "low"


def test_agent_tool_completion_defaults_to_low_reasoning(monkeypatch):
    monkeypatch.setattr(settings, "agent_reasoning_effort", "low")
    monkeypatch.setattr(settings, "chat_reasoning_effort", None)
    captured = _capture(monkeypatch)
    assert _call() == "response"
    assert captured["extra_body"] == {"reasoning_effort": "low"}
    assert captured["tools"][0]["function"]["name"] == "probe"
    assert captured["tool_choice"] == "auto"


def test_agent_reasoning_effort_overrides_chat_effort(monkeypatch):
    monkeypatch.setattr(settings, "chat_reasoning_effort", "high")
    monkeypatch.setattr(settings, "agent_reasoning_effort", "medium")
    captured = _capture(monkeypatch)
    _call()
    assert captured["extra_body"] == {"reasoning_effort": "medium"}


def test_empty_agent_effort_falls_back_to_chat_effort(monkeypatch):
    monkeypatch.setattr(settings, "chat_reasoning_effort", "high")
    monkeypatch.setattr(settings, "agent_reasoning_effort", "")
    captured = _capture(monkeypatch)
    _call()
    assert captured["extra_body"] == {"reasoning_effort": "high"}


def test_empty_agent_and_chat_effort_omit_reasoning(monkeypatch):
    monkeypatch.setattr(settings, "chat_reasoning_effort", None)
    monkeypatch.setattr(settings, "agent_reasoning_effort", "")
    captured = _capture(monkeypatch)
    _call()
    assert "extra_body" not in captured
