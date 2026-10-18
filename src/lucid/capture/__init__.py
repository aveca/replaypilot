"""Screen, window, process, and accessibility capture."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from lucid.capture.a11y import capture_a11y_tree
from lucid.capture.screenshot import ScreenshotGrabber, save_image
from lucid.capture.windows import ActiveWindow, list_windows

log = logging.getLogger("lucid.capture")


@dataclass
class MonitorInfo:
    """One physical display in the user's setup.

    ``bounds`` is ``(left, top, width, height)`` in Windows virtual-screen
    coordinates so Lucid can calculate "which monitor is to the right of
    the current one" and mouse_move the cursor there.
    """

    index: int  # mss-style index (1-based; 0 is the virtual union)
    bounds: tuple[int, int, int, int]
    is_primary: bool = False
    cursor_here: bool = False
    label: str = ""  # free-form hint, e.g. "left", "center primary"


@dataclass
class ContextSnapshot:
    """Everything we know about the desktop at one moment in time."""

    image: Image.Image
    image_path: Path | None
    monitor_index: int
    active: ActiveWindow | None
    monitor_bounds: tuple[int, int, int, int] | None = None
    windows: list[dict[str, Any]] = field(default_factory=list)
    a11y_tree: dict[str, Any] | None = None
    monitors: list[MonitorInfo] = field(default_factory=list)

    @classmethod
    def capture(cls, settings: Any, *, force_screenshot: bool = False) -> ContextSnapshot:
        """Snapshot the desktop honouring the configured capture mode.

        ``capture.mode = "vision"`` -- always take a screenshot (legacy default).
        ``capture.mode = "a11y_only"`` -- never take a screenshot, only the
                                          accessibility tree (cheap, text-only
                                          tasks). Returns a blank image so
                                          downstream code that expects one
                                          continues to work.
        ``capture.mode = "hybrid"``  -- take a11y first; only take a screenshot
                                        when the a11y tree is empty / sparse OR
                                        the caller passed ``force_screenshot``.

        ``capture.cheap_mode`` -- when True, skip the screenshot regardless
        of mode (the model is told to rely on text alone for this turn).
        """
        grabber = ScreenshotGrabber(max_width=settings.screenshot.max_width)
        active = ActiveWindow.current()
        blacklist = tuple(settings.screenshot.blacklist_titles or ())

        capture_cfg = getattr(settings, "capture", None)
        mode = (getattr(capture_cfg, "mode", "vision") or "vision").lower()
        cheap = bool(getattr(capture_cfg, "cheap_mode", False))

        if active and active.matches_blacklist(blacklist):
            log.info("Active window matches blacklist, skipping screenshot")
            img = grabber.blank()
            return cls(
                image=img,
                image_path=None,
                monitor_index=0,
                active=active,
                monitor_bounds=None,
                windows=[],
                a11y_tree=None,
            )

        # Scope the accessibility tree to the active window, not the whole
        # desktop. capture_a11y_tree() accepts an hwnd for exactly this, but it
        # used to be called without one, so GetFocusedControl() fell back to
        # GetRootControl() and the tree contained every visible application.
        #
        # That is a safety problem, not just noise: the model can only ask for
        # clicks on elements it has been shown. Measured during validation —
        # with a desktop-wide tree and the user's browser in the foreground,
        # the model requested clicks on the browser's video player controls
        # ("Pause (k)", "Disable sound (m)", "Volume") while trying to obey a
        # goal aimed at a different window. A desktop-wide tree hands it those
        # targets; a window-scoped tree never does.
        #
        # If the active window cannot be identified, the tree stays empty
        # rather than falling back to the desktop: not knowing what is in
        # front is a reason to show the model less, not more.
        a11y = (
            capture_a11y_tree(hwnd=active.hwnd)
            if settings.capture_a11y and active is not None
            else None
        )

        take_screenshot = True
        if cheap or mode == "a11y_only":
            take_screenshot = False
        elif mode == "hybrid":
            if force_screenshot:
                take_screenshot = True
            elif _a11y_is_substantial(a11y):
                take_screenshot = False

        if take_screenshot:
            img, monitor_index, monitor_bounds = grabber.grab()
            img_path = (
                save_image(img, settings.screenshot_dir) if settings.screenshot.persist else None
            )
        else:
            img = grabber.blank()
            monitor_index = 0
            monitor_bounds = None
            img_path = None

        windows = list_windows()
        monitors = _enumerate_monitors(active_index=monitor_index)
        return cls(
            image=img,
            image_path=img_path,
            monitor_index=monitor_index,
            active=active,
            monitor_bounds=monitor_bounds,
            windows=windows,
            a11y_tree=a11y,
            monitors=monitors,
        )

    def image_to_screen(self, x: int, y: int) -> tuple[int, int]:
        """Translate a coordinate from image space to absolute screen pixels.

        Claude sees a downscaled screenshot; the tool schema tells it the
        display dimensions equal the image dimensions. Its output coordinates
        are therefore in image space and must be scaled + offset to real
        monitor coordinates before we click (critical for multi-monitor).
        """
        if not self.monitor_bounds or self.image.width == 0 or self.image.height == 0:
            return (x, y)
        left, top, width, height = self.monitor_bounds
        sx = left + int(round(x * width / self.image.width))
        sy = top + int(round(y * height / self.image.height))
        return (sx, sy)

    def to_prompt_context(self) -> str:
        lines = []
        if self.active:
            lines.append(f"Active window: {self.active.title} ({self.active.process})")
        if self.monitors:
            lines.append(_format_monitor_roster(self.monitors))
        if self.windows:
            lines.append("Other windows:")
            for w in self.windows[:10]:
                lines.append(f"  - {w['title']} ({w.get('process', '?')})")
        return "\n".join(lines)


def _a11y_is_substantial(tree: dict[str, Any] | None) -> bool:
    """Return True when the a11y tree has enough text for the model to act on alone.

    Conservative heuristic: prefer a screenshot unless the tree clearly has
    real content (many nodes or non-trivial text). This means hybrid mode
    only saves a screenshot when we are confident the model will not need it.
    """
    if not tree:
        return False
    text = _flatten_a11y_text(tree)
    return len(text) >= 200


def _flatten_a11y_text(tree: Any) -> str:
    if isinstance(tree, dict):
        parts: list[str] = []
        for key in ("name", "value", "text"):
            v = tree.get(key)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        for child in tree.get("children") or []:
            parts.append(_flatten_a11y_text(child))
        return " ".join(p for p in parts if p)
    if isinstance(tree, list):
        return " ".join(_flatten_a11y_text(item) for item in tree)
    return ""


def _enumerate_monitors(active_index: int) -> list[MonitorInfo]:
    """List every physical display with its absolute bounds.

    mss.monitors[0] is the *virtual* union of all screens; entries 1..N are
    the real monitors. Primary detection: on Windows the primary monitor
    always starts at (0, 0) in the virtual coordinate space.
    """
    try:
        import mss
    except ImportError:
        return []
    out: list[MonitorInfo] = []
    try:
        with mss.mss() as sct:
            for idx, mon in enumerate(sct.monitors):
                if idx == 0:
                    continue  # skip the virtual union
                bounds = (
                    int(mon.get("left", 0)),
                    int(mon.get("top", 0)),
                    int(mon.get("width", 0)),
                    int(mon.get("height", 0)),
                )
                is_primary = bounds[0] == 0 and bounds[1] == 0
                out.append(
                    MonitorInfo(
                        index=idx,
                        bounds=bounds,
                        is_primary=is_primary,
                        cursor_here=(idx == active_index),
                        label=_monitor_label(bounds, is_primary, out),
                    )
                )
    except Exception as exc:
        log.debug("monitor enumeration failed: %s", exc)
        return []
    return out


def _monitor_label(
    bounds: tuple[int, int, int, int], is_primary: bool, previous: list[MonitorInfo]
) -> str:
    """Short positional hint ('left', 'center primary', 'right', 'above')
    so the LLM can interpret 'do X on the right monitor' prompts."""
    left = bounds[0]
    if is_primary:
        return "primary"
    # Compare to previously-enumerated monitors (mss order is platform-defined)
    prev_left_primary = next((m.bounds[0] for m in previous if m.is_primary), 0)
    if left < prev_left_primary:
        return "left"
    if left > prev_left_primary:
        return "right"
    return "secondary"


def _format_monitor_roster(monitors: list[MonitorInfo]) -> str:
    """One-liner per monitor for the prompt context block."""
    lines = [f"Monitors ({len(monitors)} total):"]
    for m in monitors:
        left, top, w, h = m.bounds
        tags = []
        if m.is_primary:
            tags.append("primary")
        if m.cursor_here:
            tags.append("CURSOR HERE")
        if m.label and m.label not in ("primary",):
            tags.append(m.label)
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"  - #{m.index}: {w}x{h} @ ({left}, {top}){tag_str}")
    lines.append(
        "  (to work on a different monitor, emit `focus_monitor` with the "
        "target index — Lucid will move the cursor there and the next "
        "screenshot will be of that display)"
    )
    return "\n".join(lines)


__all__ = [
    "ContextSnapshot",
    "MonitorInfo",
    "ScreenshotGrabber",
    "ActiveWindow",
    "list_windows",
    "capture_a11y_tree",
]
