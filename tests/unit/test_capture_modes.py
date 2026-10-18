"""Tests for the hybrid capture mode logic.

We avoid touching the real desktop by stubbing the ScreenshotGrabber and
ActiveWindow used inside ``ContextSnapshot.capture``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from lucid.capture import (
    ContextSnapshot,
    _a11y_is_substantial,
    _flatten_a11y_text,
)


def _settings(
    capture_mode: str = "vision",
    cheap: bool = False,
    capture_a11y: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        screenshot=SimpleNamespace(
            max_width=400,
            persist=False,
            retention_hours=1,
            blacklist_titles=[],
        ),
        capture=SimpleNamespace(
            mode=capture_mode,
            cheap_mode=cheap,
            a11y_max_chars=12000,
        ),
        capture_a11y=capture_a11y,
        screenshot_dir=None,
    )


@pytest.fixture
def grabber_stub(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state = {"grab_calls": 0}

    class _Grabber:
        def __init__(self, *_: Any, **__: Any) -> None: ...

        def blank(self) -> Image.Image:
            return Image.new("RGB", (1, 1), "white")

        def grab(self) -> tuple[Image.Image, int, tuple[int, int, int, int]]:
            state["grab_calls"] += 1
            return Image.new("RGB", (200, 100), "white"), 1, (0, 0, 200, 100)

    monkeypatch.setattr("lucid.capture.ScreenshotGrabber", _Grabber)
    monkeypatch.setattr("lucid.capture.ActiveWindow", _FakeActive)
    monkeypatch.setattr("lucid.capture.list_windows", lambda: [])
    monkeypatch.setattr("lucid.capture._enumerate_monitors", lambda active_index: [])
    monkeypatch.setattr(
        "lucid.capture.capture_a11y_tree", lambda hwnd=None: {"name": "root"}
    )
    return state


class _FakeActive:
    def __init__(self) -> None:
        self.title = "Test Window"
        self.process = "test.exe"
        self.hwnd = 99999
        self.pid = 1234

    @classmethod
    def current(cls) -> "_FakeActive":
        return cls()

    def matches_blacklist(self, _: tuple[str, ...]) -> bool:
        return False


def test_vision_mode_takes_screenshot(grabber_stub: dict[str, Any]) -> None:
    snap = ContextSnapshot.capture(_settings(capture_mode="vision"))
    assert grabber_stub["grab_calls"] == 1
    assert snap.image.size == (200, 100)


def test_a11y_only_skips_screenshot(grabber_stub: dict[str, Any]) -> None:
    snap = ContextSnapshot.capture(_settings(capture_mode="a11y_only"))
    assert grabber_stub["grab_calls"] == 0
    assert snap.image.size == (1, 1)
    assert snap.image_path is None


def test_cheap_mode_overrides_vision(grabber_stub: dict[str, Any]) -> None:
    snap = ContextSnapshot.capture(_settings(capture_mode="vision", cheap=True))
    assert grabber_stub["grab_calls"] == 0
    assert snap.image.size == (1, 1)


def test_hybrid_with_substantial_a11y_skips_screenshot(
    grabber_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    big_tree = {"name": "x" * 250}
    monkeypatch.setattr("lucid.capture.capture_a11y_tree", lambda hwnd=None: big_tree)
    snap = ContextSnapshot.capture(_settings(capture_mode="hybrid"))
    assert grabber_stub["grab_calls"] == 0
    assert snap.image.size == (1, 1)


def test_hybrid_with_sparse_a11y_takes_screenshot(grabber_stub: dict[str, Any]) -> None:
    """Default capture_a11y_tree from the fixture returns just {'name': 'root'}."""
    snap = ContextSnapshot.capture(_settings(capture_mode="hybrid"))
    assert grabber_stub["grab_calls"] == 1
    assert snap.image.size == (200, 100)


def test_hybrid_force_screenshot_overrides_a11y_check(
    grabber_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    big_tree = {"name": "x" * 250}
    monkeypatch.setattr("lucid.capture.capture_a11y_tree", lambda hwnd=None: big_tree)
    snap = ContextSnapshot.capture(_settings(capture_mode="hybrid"), force_screenshot=True)
    assert grabber_stub["grab_calls"] == 1
    assert snap.image.size == (200, 100)


def test_a11y_is_substantial_threshold() -> None:
    assert _a11y_is_substantial(None) is False
    assert _a11y_is_substantial({}) is False
    assert _a11y_is_substantial({"name": "tiny"}) is False
    assert _a11y_is_substantial({"name": "x" * 250}) is True


def test_flatten_a11y_text_walks_children() -> None:
    tree = {
        "name": "root",
        "children": [
            {"name": "child1"},
            {"value": "child2"},
            [{"text": "leaf"}],
        ],
    }
    flat = _flatten_a11y_text(tree)
    assert "root" in flat and "child1" in flat and "child2" in flat and "leaf" in flat
