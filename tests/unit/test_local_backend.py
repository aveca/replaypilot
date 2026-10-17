"""Regression tests for the local (lm_studio / Ollama) message translation.

The bug these cover: `LMStudioProvider.image_block()` returns the OpenAI shape
`{"type": "image_url", "image_url": {...}}`, but `_to_openai_message()` only
matched the Anthropic shape `{"type": "image", "source": {...}}`. The image
block therefore matched no branch and was dropped, so screenshots never
reached the model and any vision request was answered blind.

Run:  .venv\\Scripts\\python.exe -m pytest tests/unit/test_local_backend.py -q
"""

from __future__ import annotations

import base64
import io

from PIL import Image

from lucid.backend.lm_studio_backend import (
    LMStudioProvider,
    _to_openai_message,
    _to_openai_tool,
)
from lucid.llm.provider import Message

BLUE_PNG = "data:image/png;base64,"


def _blue_png_data_url() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 90, 200)).save(buf, format="PNG")
    return BLUE_PNG + base64.b64encode(buf.getvalue()).decode("ascii")


def test_provider_image_block_uses_openai_shape() -> None:
    provider = LMStudioProvider("http://localhost:11434/v1", "k", "m")
    block = provider.image_block(Image.new("RGB", (8, 8), (1, 2, 3)))
    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_image_block_survives_conversion() -> None:
    """The exact regression: an image_url block must not be dropped."""
    provider = LMStudioProvider("http://localhost:11434/v1", "k", "m")
    url = _blue_png_data_url()
    msg = Message(
        role="user",
        content=[
            {"type": "image_url", "image_url": {"url": url}},
            provider.text_block("What colour is this?"),
        ],
    )
    out = _to_openai_message(msg)
    assert len(out) == 1, out
    parts = out[0]["content"]
    assert isinstance(parts, list), parts
    kinds = [p["type"] for p in parts]
    assert "image_url" in kinds, f"image dropped, got {kinds}"
    image_parts = [p for p in parts if p["type"] == "image_url"]
    assert image_parts[0]["image_url"]["url"] == url


def test_anthropic_image_block_still_converts() -> None:
    """The pre-existing Anthropic path must keep working."""
    url = _blue_png_data_url()
    data = url.split(",", 1)[1]
    msg = Message(
        role="user",
        content=[
            {"type": "image", "source": {"media_type": "image/png", "data": data}},
            {"type": "text", "text": "What colour is this?"},
        ],
    )
    out = _to_openai_message(msg)
    parts = out[0]["content"]
    kinds = [p["type"] for p in parts]
    assert kinds == ["image_url", "text"], kinds
    assert parts[0]["image_url"]["url"] == f"data:image/png;base64,{data}"


def test_both_image_shapes_coexist_with_text() -> None:
    """Two images plus text must all survive, in order."""
    u1, u2 = _blue_png_data_url(), _blue_png_data_url()
    msg = Message(
        role="user",
        content=[
            {"type": "text", "text": "first"},
            {"type": "image_url", "image_url": {"url": u1}},
            {"type": "image_url", "image_url": {"url": u2}},
            {"type": "text", "text": "second"},
        ],
    )
    parts = _to_openai_message(msg)[0]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url", "image_url", "text"]
    assert parts[1]["image_url"]["url"] == u1
    assert parts[2]["image_url"]["url"] == u2


def test_tool_definition_translation() -> None:
    tool = _to_openai_tool(
        {
            "name": "computer",
            "description": "Control the computer",
            "input_schema": {"type": "object", "properties": {"action": {"type": "string"}}},
        }
    )
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "computer"
    assert tool["function"]["parameters"]["type"] == "object"