"""Regression tests: a JSON action written in prose must still drive Execute.

Measured on this machine: with a large accessibility tree in context,
llama3.1:8b answers with the action as JSON *in the content* instead of on the
native tool_calls channel. The executor saw "no action emitted", nudged four
times, and the task ended with nothing done -- while the model had in fact
emitted perfectly parseable actions:

    {"name": "click_element", "parameters": {"element_name": "TaskField"}}

Run:  .venv\\Scripts\\python.exe -m pytest tests/unit/test_text_action_recovery.py -q
"""

from __future__ import annotations

import json
from typing import Any

from lucid.backend.lm_studio_backend import (
    LMStudioProvider,
    _normalise_text_action,
)


class _FakeDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeChoice:
    def __init__(self, delta, finish=None):
        self.delta = delta
        self.finish_reason = finish


class _FakeChunk:
    def __init__(self, choice):
        self.choices = [choice]


class _FakeStream:
    """Minimal iterable stand-in for the OpenAI streamed response."""

    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)


class _NoToolClient:
    """Stand-in for the OpenAI client that always answers with prose."""

    class _Completions:
        def __init__(self, content: str) -> None:
            self._content = content

        def create(self, **kwargs):
            return _FakeStream(
                [_FakeChunk(_FakeChoice(_FakeDelta(content=self._content), finish="stop"))]
            )

    class _Chat:
        def __init__(self, completions):
            self.completions = completions

    def __init__(self, content: str) -> None:
        self.chat = self._Chat(self._Completions(content))


def _provider(monkeypatch, content: str) -> LMStudioProvider:
    provider = LMStudioProvider("http://localhost:11434/v1", "k", "llama3.1:8b")
    provider._probed = True
    provider._client = _NoToolClient(content)

    # Bypass the real HTTP client: stream() fetches a client via _get_client.
    monkeypatch.setattr(provider, "_get_client", lambda: provider._client)
    return provider


def _collect(monkeypatch, content: str):
    provider = _provider(monkeypatch, content)
    text, tools = [], []
    msgs = [
        Message(role="user", content=[provider.text_block("click the field")])
    ]
    for ev in provider.stream(msgs, tools=[_tool()], max_tokens=64):
        if ev.kind == "text_delta":
            text.append(ev.text)
        elif ev.kind == "tool_use":
            tools.append(ev.tool_use)
        elif ev.kind == "error":
            raise AssertionError(f"unexpected error event: {ev.error}")
    return "".join(text), tools


def _tool() -> dict[str, Any]:
    return {
        "name": "computer",
        "description": "Control the computer",
        "input_schema": {"type": "object", "properties": {}},
    }


from lucid.llm.provider import Message  # noqa: E402


# --------------------------------------------------------------------------
# The normaliser itself
# --------------------------------------------------------------------------


def test_nested_parameters_shape() -> None:
    """The exact shape observed in the failing live run."""
    block = _normalise_text_action(
        '{"name": "click_element", "parameters": {"element_name": "TaskField"}}'
    )
    assert block is not None
    assert block.action == "click_element"
    assert block.raw["input"] == {"element_name": "TaskField"}
    assert block.raw["_recovered_from_text"] is True


def test_flat_shape() -> None:
    block = _normalise_text_action('{"action": "type", "text": "EXECUTEOK-42"}')
    assert block is not None
    assert block.action == "type"
    assert block.raw["input"] == {"text": "EXECUTEOK-42"}


def test_stringified_coordinate_is_decoded() -> None:
    """Models frequently stringify the pair; a string coordinate is useless."""
    block = _normalise_text_action(
        '{"name": "left_click", "parameters": {"coordinate": "[462, 277]"}}'
    )
    assert block is not None
    assert block.coordinate == (462, 277)
    assert isinstance(block.raw["input"]["coordinate"], list)


def test_dotted_keys_string_is_split() -> None:
    block = _normalise_text_action('{"action": "key", "parameters": {"keys": "ctrl+a"}}')
    assert block is not None
    assert block.raw["input"]["keys"] == ["ctrl", "a"]


def test_prose_is_never_an_action() -> None:
    assert _normalise_text_action("I think we should click the button now.") is None
    assert _normalise_text_action("Sure! Let me help with that.") is None


def test_non_object_json_is_never_an_action() -> None:
    assert _normalise_text_action("[1, 2, 3]") is None
    assert _normalise_text_action('"click_element"') is None


def test_object_without_action_name_is_never_an_action() -> None:
    assert _normalise_text_action('{"reason": "just a note"}') is None
    assert _normalise_text_action('{"name": 123}') is None


def test_markdown_fence_is_stripped() -> None:
    block = _normalise_text_action(
        '```json\n{"name": "click_element", "parameters": {"element_name": "Submit"}}\n```'
    )
    assert block is not None
    assert block.action == "click_element"


def test_contradictory_inner_action_key_is_removed() -> None:
    block = _normalise_text_action(
        '{"name": "type", "parameters": {"action": "key", "text": "x"}}'
    )
    assert block is not None
    assert block.action == "type"
    assert "action" not in block.raw["input"]


# --------------------------------------------------------------------------
# End of stream: recovery only when native tool_calls are absent
# --------------------------------------------------------------------------


def test_stream_recovers_action_from_text(monkeypatch) -> None:
    content = '{"name": "click_element", "parameters": {"element_name": "TaskField"}}'
    text, tools = _collect(monkeypatch, content)
    assert len(tools) == 1, "the JSON action in the content must become a tool_use"
    assert tools[0].action == "click_element"
    # The text is still emitted, so the transcript shows what the model said.
    assert content in "".join(text)


def test_stream_does_not_invent_actions_from_prose(monkeypatch) -> None:
    text, tools = _collect(monkeypatch, "Nothing to do here, the task looks done.")
    assert tools == [], "prose must not become a desktop action"
    assert "Nothing to do" in "".join(text)


def test_stream_with_native_tool_calls_is_untouched(monkeypatch) -> None:
    """A real tool_call must never be shadowed by the recovery path."""
    provider = LMStudioProvider("http://localhost:11434/v1", "k", "m")
    provider._probed = True

    class _TC:
        index = 0
        id = "call_1"

        class function:
            name = "computer"
            arguments = '{"action": "click_element", "element_name": "Submit"}'

    class _Completions:
        def create(self, **kwargs):
            return _FakeStream(
                [
                    _FakeChunk(_FakeChoice(_FakeDelta(tool_calls=[_TC()]), finish="tool_calls")),
                ]
            )

    class _Chat:
        def __init__(self, completions):
            self.completions = completions

    class _Client:
        chat = _Chat(_Completions())

    provider._client = _Client()
    monkeypatch.setattr(provider, "_get_client", lambda: provider._client)

    msgs = [Message(role="user", content=[provider.text_block("go")])]
    tools = []
    for ev in provider.stream(msgs, tools=[_tool()], max_tokens=64):
        if ev.kind == "tool_use":
            tools.append(ev.tool_use)
    assert len(tools) == 1
    assert tools[0].raw.get("_recovered_from_text") is not True
    assert tools[0].action == "click_element"