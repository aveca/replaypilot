"""Foreground-window guard for any harness that types into a real desktop.

This module exists because a real failure was observed during validation:
`Actions._focus_window` calls `SetForegroundWindow`, which Windows can refuse
when the calling process is not the foreground process. The action then
reports a failure but the *next* input action still fires, and pyautogui
delivers the keystrokes to whatever window happens to be in front. In our run
that window was World of Warcraft.

Two rules are enforced here:

1. A hard denylist. Any foreground window matching it aborts the run. The
   guard exists specifically so automation can never type into a game.
2. An allowlist. When an expected window title is supplied, every input
   action is preceded by a check that the foreground window still matches.
   A mismatch aborts instead of typing.

Nothing here types, clicks or moves the mouse. It only reads window titles.
"""

from __future__ import annotations

import sys
import time

# Windows whose title indicates a game client. Never type into these.
GAME_DENYLIST: tuple[str, ...] = (
    "world of warcraft",
    "wowclassic",
    "wow",
    "warcraft",
)

# Titles that indicate a credential prompt. Never type into these either.
SECRET_DENYLIST: tuple[str, ...] = (
    "bitwarden",
    "1password",
    "keepass",
    "credential",
    "password",
    "sign-in",
    "login",
)


class FocusViolation(RuntimeError):
    """Raised when input must not proceed for the current foreground window."""


def _foreground_title() -> str:
    import win32gui

    return win32gui.GetWindowText(win32gui.GetForegroundWindow()) or ""


def check(expected: str | None = None, *, deny_games: bool = True) -> str:
    """Return the foreground title, or raise FocusViolation.

    ``expected`` is a case-insensitive substring the foreground title must
    contain. Pass it before every input action.
    """
    title = _foreground_title()
    low = title.lower()

    if deny_games:
        for bad in GAME_DENYLIST:
            if bad in low:
                raise FocusViolation(
                    f"REFUSING TO TYPE: foreground window {title!r} looks like a game "
                    f"(matched denylist {bad!r}). Automation halted."
                )

    for bad in SECRET_DENYLIST:
        if bad in low:
            raise FocusViolation(
                f"REFUSING TO TYPE: foreground window {title!r} looks like a credential "
                f"prompt (matched denylist {bad!r}). Automation halted."
            )

    if expected and expected.lower() not in low:
        raise FocusViolation(
            f"REFUSING TO TYPE: expected a window containing {expected!r}, "
            f"foreground is {title!r}. Automation halted."
        )

    return title


def guarded(action, expected: str, settle: float = 0.35, **params) -> str:
    """Check focus, then run exactly one input action, then re-check.

    A window that steals focus between the pre-check and the keystroke is
    still possible, so this keeps the window title stable across the action
    and raises if it changed unexpectedly.
    """
    check(expected)
    time.sleep(settle)
    check(expected)
    result = action(**params)
    time.sleep(settle)
    after = check(expected)
    return f"{result} [fg={after!r}]"


def describe_environment() -> str:
    """Report what the guard considers risky right now. Read-only."""
    title = _foreground_title()
    low = title.lower()
    flags = []
    for bad in GAME_DENYLIST:
        if bad in low:
            flags.append(f"GAME({bad})")
    for bad in SECRET_DENYLIST:
        if bad in low:
            flags.append(f"SECRET({bad})")
    verdict = ",".join(flags) if flags else "clear"
    return f"foreground={title!r} verdict={verdict}"


def find_window(title_substring: str) -> int:
    """HWND of the visible top-level window whose title contains the substring."""
    import win32gui

    needle = title_substring.lower()
    found: list[int] = []

    def _enum(hwnd: int, _extra) -> bool:
        if win32gui.IsWindowVisible(hwnd):
            t = (win32gui.GetWindowText(hwnd) or "").lower()
            if needle in t:
                found.append(hwnd)
        return True

    win32gui.EnumWindows(_enum, None)
    if not found:
        raise FocusViolation(f"no visible window matching {title_substring!r}")
    return found[0]


def force_foreground(hwnd: int) -> bool:
    """Bring a window we own to the front, reliably.

    `SetForegroundWindow` alone is refused by Windows when the calling process
    is not the foreground process, which is exactly what we hit. The
    documented workaround is to attach our input queue to the foreground
    thread's queue for the duration of the call.

    This only raises windows. It never sends input to any window.
    """
    import win32api
    import win32con
    import win32gui
    import win32process

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    else:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

    fg = win32gui.GetForegroundWindow()
    cur_tid = win32api.GetCurrentThreadId()
    fg_tid = win32process.GetWindowThreadProcessId(fg)[0] if fg else cur_tid

    attached = False
    try:
        if fg_tid and fg_tid != cur_tid:
            win32process.AttachThreadInput(cur_tid, fg_tid, True)
            attached = True
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
        win32gui.SetActiveWindow(hwnd)
    except Exception:
        return False
    finally:
        if attached:
            try:
                win32process.AttachThreadInput(cur_tid, fg_tid, False)
            except Exception:
                pass

    time.sleep(0.3)
    return _foreground_title().lower() in win32gui.GetWindowText(hwnd).lower()


def bring_forward_and_verify(title_substring: str, attempts: int = 3) -> str:
    """Raise the given window to the front and confirm it owns the foreground.

    Retries a few times because another window may steal focus back. Raises
    FocusViolation if the window never ends up in front, which is the signal
    the caller must not type anything.
    """
    last = ""
    for _ in range(attempts):
        hwnd = find_window(title_substring)
        force_foreground(hwnd)
        time.sleep(0.4)
        try:
            return check(title_substring)
        except FocusViolation as exc:
            last = str(exc)
            time.sleep(0.6)
    raise FocusViolation(last or f"could not bring {title_substring!r} to the foreground")


if __name__ == "__main__":
    print(describe_environment())
    print(f"python={sys.version.split()[0]}")