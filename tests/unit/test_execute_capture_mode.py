"""Regression tests: Execute mode must honour capture.mode.

The defect: `ExecuteMode._loop` did
`content = [self.provider.image_block(snapshot.image)]` unconditionally, on the
first turn, on every follow-up, and in every tool_result. So a text-only local
model was sent a 16x16 blank frame and rejected the entire request:

    Multimodal data provided, but model does not support multimodal requests.

even with `capture.mode: a11y_only` configured precisely to avoid images.

Run:  .venv\\Scripts\\python.exe -m pytest tests/unit/test_execute_capture_mode.py -q
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

from PIL import Image

from lucid.agent.execute_mode import ExecuteMode
from lucid.config.settings import Settings
from lucid.llm.provider import LLMProvider
from lucid.llm.schemas import StreamEvent


class _CapturingProvider(LLMProvider):
    name = "capture"

    def __init__(self) -> None:
        self.contents: list[list] = []

    def stream(self, messages, *, system=None, tools=None, max_tokens=2048,
               model=None, cache_system=False, cache_tools=False) -> Iterator[StreamEvent]:
        self.contents.append([list(m.content) for m in messages])
        yield StreamEvent(kind="text_delta", text="ok")
        yield StreamEvent(kind="done", stop_reason="end_turn")

    def image_block(self, img) -> dict[str, Any]:
        return {"type": "image", "w": img.width, "h": img.height}

    def text_block(self, text: str) -> dict[str, Any]:
        return {"type": "text", "text": text}

    def tool_result_block(self, tool_use_id, content, is_error=False):
        return {"type": "tool_result"}


class _FakeSnapshot:
    def __init__(self, image: Image.Image) -> None:
        self.image = image
        self.a11y_tree = {"role": "window", "name": "Fake", "children": []}
        self.active = None
        self.image_path = None
        self.monitor_index = 0
        self.monitor_bounds = None
        self.windows = []
        self.monitors = []

    def to_prompt_context(self) -> str:
        return "Screen context: fake"


def _run(settings: Settings, snapshot: _FakeSnapshot, attachments=None) -> _CapturingProvider:
    provider = _CapturingProvider()
    execute = ExecuteMode(settings, provider)
    with patch.object(ExecuteMode, "_final_proof", return_value=iter(())):
        list(execute.run("Do the thing.", snapshot, threading.Event(),
                         attachments=attachments))
    return provider


def _blocks(provider: _CapturingProvider, kind: str, *, first_turn: bool = True) -> list[dict]:
    """Blocks of a given type, from the first turn by default.

    `stream()` stores `[list(m.content) for m in messages]` per call, so the
    structure is turns -> messages -> blocks, three levels deep.

    Assertions are scoped to the first turn because the Execute loop
    re-captures and re-sends on every turn; counting across turns makes the
    expectation depend on how many turns the fake provider happens to allow.
    """
    turns = provider.contents[:1] if first_turn else provider.contents
    return [
        b
        for turn in turns
        for msg in turn
        for b in msg
        if isinstance(b, dict) and b.get("type") == kind
    ]


def _images(provider: _CapturingProvider) -> list[dict]:
    return _blocks(provider, "image")


def _texts(provider: _CapturingProvider) -> list[dict]:
    return _blocks(provider, "text")


# --------------------------------------------------------------------------
# a11y_only: no image may reach the model
# --------------------------------------------------------------------------


def test_a11y_only_sends_no_image() -> None:
    s = Settings()
    s.capture.mode = "a11y_only"
    s.memory.enabled = False
    provider = _run(s, _FakeSnapshot(Image.new("RGB", (64, 48), (1, 2, 3))))
    assert _images(provider) == [], "an image was attached despite a11y_only"


def test_cheap_mode_sends_no_image() -> None:
    s = Settings()
    s.capture.mode = "vision"
    s.capture.cheap_mode = True
    s.memory.enabled = False
    provider = _run(s, _FakeSnapshot(Image.new("RGB", (64, 48), (1, 2, 3))))
    assert _images(provider) == [], "cheap_mode must suppress the image"


def test_a11y_only_still_sends_text_context() -> None:
    """Skipping the image must not skip the context the model needs."""
    s = Settings()
    s.capture.mode = "a11y_only"
    s.memory.enabled = False
    provider = _run(s, _FakeSnapshot(Image.new("RGB", (64, 48), (1, 2, 3))))
    texts = _texts(provider)
    assert texts, "no text block at all"
    assert any("Accessibility tree" in t["text"] for t in texts), (
        "the accessibility tree must be present when the image is omitted"
    )


def test_a11y_only_omits_reference_images_too() -> None:
    """Attachments are images, so they are omitted under a11y_only as well."""
    s = Settings()
    s.capture.mode = "a11y_only"
    s.memory.enabled = False
    refs = [Image.new("RGB", (40, 50), (255, 0, 0))]
    provider = _run(s, _FakeSnapshot(Image.new("RGB", (64, 48), (1, 2, 3))), attachments=refs)
    assert _images(provider) == []


# --------------------------------------------------------------------------
# vision mode keeps the existing behaviour
# --------------------------------------------------------------------------


def test_vision_mode_still_sends_the_live_screenshot() -> None:
    s = Settings()
    s.capture.mode = "vision"
    s.memory.enabled = False
    provider = _run(s, _FakeSnapshot(Image.new("RGB", (64, 48), (1, 2, 3))))
    images = _images(provider)
    assert len(images) == 1, f"expected exactly one live screenshot, got {len(images)}"
    assert (images[0]["w"], images[0]["h"]) == (64, 48)


def test_vision_mode_still_sends_reference_images() -> None:
    s = Settings()
    s.capture.mode = "vision"
    s.memory.enabled = False
    refs = [Image.new("RGB", (40, 50), (255, 0, 0)), Image.new("RGB", (60, 70), (0, 255, 0))]
    provider = _run(s, _FakeSnapshot(Image.new("RGB", (64, 48), (1, 2, 3))), attachments=refs)
    images = _images(provider)
    assert len(images) == 3
    assert (images[0]["w"], images[0]["h"]) == (64, 48)
    assert (images[1]["w"], images[1]["h"]) == (40, 50)
    assert (images[2]["w"], images[2]["h"]) == (60, 70)


# --------------------------------------------------------------------------
# The blank placeholder specifically
# --------------------------------------------------------------------------


def test_blank_placeholder_is_not_attached() -> None:
    """ScreenshotGrabber.blank() is 16x16; sending it is pure waste."""
    s = Settings()
    s.capture.mode = "vision"
    s.memory.enabled = False
    provider = _run(s, _FakeSnapshot(Image.new("RGB", (16, 16), (0, 0, 0))))
    assert _images(provider) == [], "the blank placeholder should not be sent"


def test_small_but_real_screenshot_is_attached() -> None:
    """32x24 is a real frame, not the placeholder. Guard against over-filtering."""
    s = Settings()
    s.capture.mode = "vision"
    s.memory.enabled = False
    provider = _run(s, _FakeSnapshot(Image.new("RGB", (32, 24), (9, 9, 9))))
    assert len(_images(provider)) == 1, "a genuine small screenshot must still be sent"


# --------------------------------------------------------------------------
# Direct unit checks on the predicate
# --------------------------------------------------------------------------


def test_may_attach_image_predicate() -> None:
    s = Settings()
    execute = ExecuteMode(s, _CapturingProvider())

    real = _FakeSnapshot(Image.new("RGB", (64, 48)))
    blank = _FakeSnapshot(Image.new("RGB", (16, 16), (0, 0, 0)))

    s.capture.mode = "vision"
    assert execute._may_attach_image(real) is True
    assert execute._may_attach_image(blank) is False

    s.capture.mode = "a11y_only"
    assert execute._may_attach_image(real) is False

    s.capture.mode = "vision"
    s.capture.cheap_mode = True
    assert execute._may_attach_image(real) is False


def test_missing_image_attribute_is_safe() -> None:
    s = Settings()
    s.capture.mode = "vision"
    execute = ExecuteMode(s, _CapturingProvider())

    class _NoImage:
        image = None
        a11y_tree = None

    assert execute._may_attach_image(_NoImage()) is False