"""Regression tests for normaliser gaps measured in the Execute dry run.

Two shapes the first recovery implementation got wrong, both observed in the
dry-run output:

  1. keys written as a Python literal string: "['ctrl','l']" -- the splitter
     only handled "ctrl+a", so the chord was dropped;
  2. a native tool_call whose arguments omit "action" --
     {"element_name": "Submit"} produced ComputerUseBlock(action=""), which
     the executor silently ignores, so a click the model did request was lost.

Run:  .venv\\Scripts\\python.exe -m pytest tests/unit/test_action_normalisation.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from lucid.backend.lm_studio_backend import (  # noqa: E402
    LMStudioProvider,
    _normalise_text_action,
    _parse_key_list,
)
from lucid.llm.provider import Message  # noqa: E402


# --------------------------------------------------------------------------
# _parse_key_list
# --------------------------------------------------------------------------


def test_python_literal_keys() -> None:
    assert _parse_key_list("['ctrl','l']") == ["ctrl", "l"]


def test_json_keys() -> None:
    assert _parse_key_list('["ctrl", "l"]') == ["ctrl", "l"]


def test_joined_keys() -> None:
    assert _parse_key_list("ctrl+a") == ["ctrl", "a"]


def test_single_key_without_separator() -> None:
    assert _parse_key_list("delete") == ["delete"]


def test_prose_is_rejected() -> None:
    assert _parse_key_list("hello world") == []


def test_empty_is_empty() -> None:
    assert _parse_key_list("") == []


def test_bracket_but_not_a_list_is_rejected() -> None:
    assert _parse_key_list('["ctrl", 12]') == ["ctrl", "12"]


# --------------------------------------------------------------------------
# recovery path with the measured shapes
# --------------------------------------------------------------------------


def test_recovery_parses_python_literal_keys() -> None:
    block = _normalise_text_action(
        '{"name": "key", "parameters": {"keys": "[\'ctrl\',\'l\']"}}'
    )
    assert block is not None
    assert block.action == "key"
    assert block.raw["input"]["keys"] == ["ctrl", "l"]


def test_recovery_drops_prose_keys_but_keeps_action() -> None:
    block = _normalise_text_action(
        '{"name": "key", "parameters": {"keys": "hello world"}}'
    )
    assert block is not None
    assert block.action == "key"
    assert "keys" not in block.raw["input"]


# --------------------------------------------------------------------------
# native tool_call with no "action" in its arguments
# --------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)


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


def _provider_with_tool_call(function_name: str, arguments: str):
    provider = LMStudioProvider("http://localhost:11434/v1", "k", "m")
    provider._probed = True

    # Built with type() rather than nested class statements: a class body two
    # levels deep cannot read this function's locals, which silently raised
    # NameError on the first version of this helper.
    tc = type("_TC", (), {"index": 0, "id": "call_x"})()
    tc.function = type(
        "_Fn", (), {"name": function_name, "arguments": arguments}
    )()

    class _Completions:
        def create(self, **kwargs):
            return _FakeStream(
                [
                    _FakeChunk(_FakeChoice(_FakeDelta(tool_calls=[tc]), finish="tool_calls"))
                ]
            )

    class _Chat:
        def __init__(self, completions):
            self.completions = completions

    class _Client:
        chat = _Chat(_Completions())

    provider._client = _Client()
    return provider


TOOL = {
    "name": "computer",
    "description": "Control the computer",
    "input_schema": {"type": "object", "properties": {}},
}


def _stream_tools(provider) -> list:
    tools = []
    msgs = [Message(role="user", content=[provider.text_block("go")])]
    for ev in provider.stream(msgs, tools=[TOOL], max_tokens=64):
        if ev.kind == "tool_use":
            tools.append(ev.tool_use)
    return tools


def test_missing_action_key_falls_back_to_function_name(monkeypatch) -> None:
    """Measured: {"element_name": "Submit"} produced an unnamed, lost action."""
    provider = _provider_with_tool_call("click_element", '{"element_name": "Submit"}')
    monkeypatch.setattr(provider, "_get_client", lambda: provider._client)
    tools = _stream_tools(provider)
    assert len(tools) == 1
    assert tools[0].action == "click_element"
    assert tools[0].raw["input"] == {"element_name": "Submit"}


def test_explicit_action_key_wins_over_function_name(monkeypatch) -> None:
    provider = _provider_with_tool_call(
        "computer", '{"action": "type", "text": "EXECUTEOK-42"}'
    )
    monkeypatch.setattr(provider, "_get_client", lambda: provider._client)
    tools = _stream_tools(provider)
    assert len(tools) == 1
    assert tools[0].action == "type"
    assert tools[0].text == "EXECUTEOK-42"


def test_action_key_with_function_name_keeping_unnamed_shape(monkeypatch) -> None:
    """No action anywhere -> still unnamed, but never a hallucinated action."""
    provider = _provider_with_tool_call("computer", '{"element_name": "x"}')
    monkeypatch.setattr(provider, "_get_client", lambda: provider._client)
    tools = _stream_tools(provider)
    assert len(tools) == 1
    assert tools[0].action == "computer"


def test_empty_arguments_with_named_function(monkeypatch) -> None:
    provider = _provider_with_tool_call("wait", "{}")
    monkeypatch.setattr(provider, "_get_client", lambda: provider._client)
    tools = _stream_tools(provider)
    assert len(tools) == 1
    assert tools[0].action == "wait"


@pytest.mark.parametrize(
    "name,args",
    [
        ("click_element", '{"element_name": "Submit"}'),
        ("computer", '{"action": "key", "keys": "[\'ctrl\',\'l\']"}'),
    ],
)
def test_no_action_is_dropped_silently(monkeypatch, name, args) -> None:
    provider = _provider_with_tool_call(name, args)
    monkeypatch.setattr(provider, "_get_client", lambda: provider._client)
    tools = _stream_tools(provider)
    assert tools, f"{name} with {args} must still yield a tool_use"
    assert tools[0].action, "action name must never be empty"