"""Active window detection and window list enumeration on Windows."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("lucid.capture.windows")

try:
    import pygetwindow as gw
except ImportError:
    gw = None  # type: ignore[assignment]

if sys.platform == "win32":
    try:
        import win32gui
        import win32process
    except ImportError:
        win32gui = None  # type: ignore[assignment]
        win32process = None  # type: ignore[assignment]
else:
    win32gui = None  # type: ignore[assignment]
    win32process = None  # type: ignore[assignment]

# psutil is optional and NOT declared in pyproject.toml. It only supplies a
# friendlier process name; everything else works without it.
#
# It must NOT share a try block with win32gui: a missing psutil used to blank
# win32gui as well, so ActiveWindow.current() returned None on every call,
# ContextSnapshot saw no active window, and the accessibility tree fell back
# to the whole desktop. The model was then shown every visible application's
# controls -- including a game and the user's browser -- and asked for clicks
# on them. One missing optional package silently disabled window scoping.
try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[assignment]


@dataclass
class ActiveWindow:
    title: str
    process: str
    pid: int
    hwnd: int
    bounds: tuple[int, int, int, int] | None = None

    @classmethod
    def current(cls) -> ActiveWindow | None:
        if win32gui is None:
            return None
        try:
            hwnd = win32gui.GetForegroundWindow()
            if hwnd == 0:
                return None
            title = win32gui.GetWindowText(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process_name = "unknown"
            if psutil is not None:
                try:
                    process_name = psutil.Process(pid).name()
                except psutil.Error:
                    pass
            rect = win32gui.GetWindowRect(hwnd)
            return cls(
                title=title,
                process=process_name,
                pid=pid,
                hwnd=hwnd,
                bounds=rect,
            )
        except Exception as exc:
            log.debug("ActiveWindow.current failed: %s", exc)
            return None

    def center(self) -> tuple[int, int]:
        if not self.bounds:
            return (0, 0)
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    def matches_blacklist(self, patterns: tuple[str, ...]) -> bool:
        title_lower = self.title.lower()
        return any(p.lower() in title_lower for p in patterns)


def list_windows(limit: int = 25) -> list[dict[str, Any]]:
    if gw is None:
        return []
    result: list[dict[str, Any]] = []
    try:
        for w in gw.getAllWindows():
            if not w.title or not w.visible:
                continue
            result.append(
                {
                    "title": w.title,
                    "left": w.left,
                    "top": w.top,
                    "width": w.width,
                    "height": w.height,
                }
            )
            if len(result) >= limit:
                break
    except Exception as exc:
        log.debug("list_windows failed: %s", exc)
    return result
