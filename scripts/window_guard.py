"""Fail-closed guard for any automation that touches the real desktop.

Written after a real incident: `docs/incidents/2026-10-17-foreground-hijack.md`.
On 2026-10-17 keystrokes intended for a local test window reached a running
World of Warcraft window, because `SetForegroundWindow` was refused, the game
took the foreground, and nothing checked who was in front before typing.

This module is the fix. It is deliberately paranoid, and every default is
"refuse".

Design rules
------------
1. **Identity, not appearance.** Authorization is bound to a concrete
   ``(HWND, PID)`` pair, never to a title substring. A title can be empty,
   change, or be duplicated by another window; an HWND cannot be confused with
   the game window.
2. **Process denylist, not just title.** World of Warcraft is recognised by
   ``WowClassic.exe`` as well as by its title. A renamed or titleless window
   still trips the block.
3. **Ownership.** A window may only be authorized if the caller passes the PID
   it expects. The guard will not raise or type into a window it was not told
   to expect.
4. **No fallback.** If the authorized window cannot be brought to the
   foreground, this raises. There is no path that types into "whatever is
   active".
5. **Check before and after.** The authorized window must own the foreground
   immediately before the input and still own it immediately after. A window
   that steals focus mid-action is detected and reported.
6. **Simulation mode.** ``set_input_enabled(False)`` makes every input call
   raise without emitting anything, so guard logic can be tested exhaustively
   on a machine that has a game client running.

Nothing in this module types, clicks or moves the mouse. It only reads window
state, unless a caller explicitly performs an action through
``GuardedSession.run_input``.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Callable

# --------------------------------------------------------------------------
# Denylists
# --------------------------------------------------------------------------

# Titles that indicate a game client. Never type into these.
GAME_DENYLIST: tuple[str, ...] = (
    "world of warcraft",
    "wowclassic",
    "warcraft",
    "wow",
    "diablo",
    "valorant",
    "counter-strike",
    "overwatch",
    "fortnite",
    "gta",
    "elden ring",
)

# Process *names* (lower case, without extension) that must never receive
# input. This is the layer that survives a title change or an empty title.
GAME_DENYLIST_PROCESSES: tuple[str, ...] = (
    "wowclassic",
    "wow",
    "worldofwarcraft",
    "wowt",
    "d4diablo",
    "diabloiv",
    "valorant",
    "cs2",
    "overwatch",
    "fortniteclient-win64-shipping",
)

# Titles that indicate a credential prompt.
SECRET_DENYLIST: tuple[str, ...] = (
    "bitwarden",
    "1password",
    "keepass",
    "credential",
    "password",
    "sign-in",
    "login",
    "logon",
)

SECRET_DENYLIST_PROCESSES: tuple[str, ...] = (
    "credentialuiproviderhost",
    "logonui",
)


class FocusViolation(RuntimeError):
    """Raised when input must not proceed. Always means: nothing was typed."""


# --------------------------------------------------------------------------
# Global simulation switch
# --------------------------------------------------------------------------

_INPUT_ENABLED = True


def set_input_enabled(enabled: bool) -> None:
    """Enable or disable all input emission through this module.

    With input disabled, ``GuardedSession.run_input`` raises
    ``FocusViolation`` *after* performing its checks. That lets the guard's
    decision logic be tested with zero risk on a machine with a game running.
    """
    global _INPUT_ENABLED
    _INPUT_ENABLED = enabled


def input_enabled() -> bool:
    return _INPUT_ENABLED


# --------------------------------------------------------------------------
# Low-level window reads
#
# All reads go through _READERS so the guard's decision logic can be tested
# exhaustively against synthetic window states, with no input and no
# dependency on what happens to be on screen. Tests replace this dict; nothing
# else needs to be monkeypatched.
# --------------------------------------------------------------------------


def _read_foreground() -> int:
    import win32gui

    return int(win32gui.GetForegroundWindow())


def _read_exists(hwnd: int) -> bool:
    import win32gui

    return bool(win32gui.IsWindow(hwnd))


def _read_title(hwnd: int) -> str:
    import win32gui

    return win32gui.GetWindowText(hwnd) or ""


def _read_pid(hwnd: int) -> int:
    import win32process

    return int(win32process.GetWindowThreadProcessId(hwnd)[1])


def _read_process(pid: int) -> str:
    """Lower-case process name for a PID, without the .exe extension.

    Uses QueryFullProcessImageNameW via ctypes so there is no extra dependency.
    psutil is used when present only because it is marginally faster.
    """
    if not pid:
        return ""
    try:
        import psutil  # type: ignore

        return (psutil.Process(pid).name() or "").lower().replace(".exe", "")
    except Exception:
        pass

    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(512)
            buf = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                wintypes.HANDLE(handle), 0, buf, ctypes.byref(size)
            ):
                return ""
            return buf.value.rsplit("\\", 1)[-1].lower().replace(".exe", "")
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(handle))
    except Exception:
        return ""


_READERS: dict[str, object] = {
    "foreground": _read_foreground,
    "exists": _read_exists,
    "title": _read_title,
    "pid": _read_pid,
    "process": _read_process,
}


def install_readers(**overrides: object) -> dict[str, object]:
    """Replace window-state readers. Returns the previous mapping."""
    previous = dict(_READERS)
    _READERS.update(overrides)
    return previous


def restore_readers(previous: dict[str, object]) -> None:
    _READERS.clear()
    _READERS.update(previous)


def foreground_snapshot() -> dict:
    """Read-only description of the current foreground window."""
    hwnd = int(_READERS["foreground"]())  # type: ignore[operator]
    pid = int(_READERS["pid"](hwnd)) if hwnd else 0  # type: ignore[operator]
    return {
        "hwnd": hwnd,
        "pid": pid,
        "title": str(_READERS["title"](hwnd)) if hwnd else "",  # type: ignore[operator]
        "process": str(_READERS["process"](pid)) if pid else "",  # type: ignore[operator]
    }


# --------------------------------------------------------------------------
# Denylist evaluation
# --------------------------------------------------------------------------


def evaluate_denylist(title: str, process: str) -> list[str]:
    """Return the reasons this window is off limits. Empty means allowed."""
    reasons: list[str] = []
    low_title = (title or "").lower()
    low_proc = (process or "").lower()

    for bad in GAME_DENYLIST:
        if bad in low_title:
            reasons.append(f"GAME_TITLE({bad})")
            break
    for bad in GAME_DENYLIST_PROCESSES:
        if low_proc and (low_proc == bad or low_proc.startswith(bad)):
            reasons.append(f"GAME_PROCESS({bad})")
            break
    for bad in SECRET_DENYLIST:
        if bad in low_title:
            reasons.append(f"SECRET_TITLE({bad})")
            break
    for bad in SECRET_DENYLIST_PROCESSES:
        if low_proc and low_proc == bad:
            reasons.append(f"SECRET_PROCESS({bad})")
            break
    return reasons


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizedWindow:
    """A window this session is permitted to interact with. Nothing else."""

    hwnd: int
    pid: int
    title: str
    process: str

    def describe(self) -> str:
        return f"hwnd={self.hwnd} pid={self.pid} title={self.title!r} process={self.process!r}"


def find_window(title_substring: str, *, expected_pid: int | None = None) -> AuthorizedWindow:
    """Locate and authorize a window.

    ``expected_pid`` is not optional in practice: callers should pass the PID
    of the process they launched. If the found window belongs to another
    process, this raises rather than authorizing it.
    """
    import win32gui

    needle = (title_substring or "").lower()
    if not needle:
        raise FocusViolation("authorize() requires a non-empty title substring")

    exists = _READERS["exists"]
    title_of = _READERS["title"]
    pid_of = _READERS["pid"]
    proc_of = _READERS["process"]

    matches: list[AuthorizedWindow] = []

    def _enum(hwnd: int, _extra) -> bool:
        if not exists(hwnd):  # type: ignore[operator]
            return True
        title = title_of(hwnd)  # type: ignore[operator]
        if needle not in (title or "").lower():
            return True
        pid = int(pid_of(hwnd))  # type: ignore[operator]
        matches.append(
            AuthorizedWindow(
                hwnd=int(hwnd),
                pid=pid,
                title=title,
                process=str(proc_of(pid)),  # type: ignore[operator]
            )
        )
        return True

    win32gui.EnumWindows(_enum, None)

    if not matches:
        raise FocusViolation(
            f"REFUSING: no visible window matching {title_substring!r} exists"
        )

    if expected_pid is not None:
        owned = [m for m in matches if m.pid == expected_pid]
        if not owned:
            found = ", ".join(m.describe() for m in matches)
            raise FocusViolation(
                f"REFUSING: window matching {title_substring!r} belongs to another "
                f"process. Expected pid {expected_pid}, found: {found}"
            )
        match = owned[0]
    else:
        match = matches[0]

    reasons = evaluate_denylist(match.title, match.process)
    if reasons:
        raise FocusViolation(
            f"REFUSING: window {match.describe()} is on a denylist ({','.join(reasons)})"
        )

    return match


def authorize(title_substring: str, *, expected_pid: int) -> AuthorizedWindow:
    """Authorize exactly one window of a process we launched.

    ``expected_pid`` is required. This is the ownership rule: the guard will
    not touch a window whose owning process the caller did not name.
    """
    if not expected_pid:
        raise FocusViolation("REFUSING: authorize() requires an explicit expected_pid")
    return find_window(title_substring, expected_pid=expected_pid)


# --------------------------------------------------------------------------
# Focus control (raises windows only, never types)
# --------------------------------------------------------------------------


def raise_window(win: AuthorizedWindow, attempts: int = 4) -> None:
    """Bring the authorized window to the foreground, or raise FocusViolation.

    Uses the documented AttachThreadInput workaround because plain
    SetForegroundWindow is refused when the caller is not the foreground
    process.

    This only raises windows. It emits no input to any window. If the window
    cannot be brought forward, this raises -- there is deliberately no fallback
    to the currently active window.
    """
    import win32api
    import win32con
    import win32gui
    import win32process

    last = ""
    exists = _READERS["exists"]
    foreground = _READERS["foreground"]
    for _ in range(attempts):
        if not exists(win.hwnd):  # type: ignore[operator]
            raise FocusViolation(
                f"REFUSING: authorized window no longer exists ({win.describe()})"
            )

        if win32gui.IsIconic(win.hwnd):
            win32gui.ShowWindow(win.hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(win.hwnd, win32con.SW_SHOW)

        fg = int(win32gui.GetForegroundWindow())
        cur_tid = win32api.GetCurrentThreadId()
        fg_tid = int(win32process.GetWindowThreadProcessId(fg)[0]) if fg else cur_tid

        attached = False
        try:
            if fg_tid and fg_tid != cur_tid:
                win32process.AttachThreadInput(cur_tid, fg_tid, True)
                attached = True
            win32gui.BringWindowToTop(win.hwnd)
            win32gui.SetForegroundWindow(win.hwnd)
            win32gui.SetActiveWindow(win.hwnd)
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        finally:
            if attached:
                try:
                    win32process.AttachThreadInput(cur_tid, fg_tid, False)
                except Exception:
                    pass

        time.sleep(0.35)
        if int(foreground()) == win.hwnd:  # type: ignore[operator]
            return
        last = last or "window did not take the foreground"

    raise FocusViolation(
        f"REFUSING: could not bring the authorized window to the foreground "
        f"after {attempts} attempts ({last}). Not falling back to the active "
        f"window. ({win.describe()})"
    )


# --------------------------------------------------------------------------
# Guarded session
# --------------------------------------------------------------------------

# Every Lucid action that emits keyboard or mouse input. This list is derived
# from the dispatch table in src/lucid/executor/actions.py::Actions.run -- if
# a new input action is added there, it must be added here or the guard will
# wrongly treat it as harmless.
#
# The easily-missed members are the semantic ones: click_element and
# scroll_into_view move the real mouse, file_dialog_paste types a path, and
# solve_captcha both clicks and types. Treating any of them as "read only"
# would be a hole in the guard.
_INPUT_ACTION_NAMES = {
    # direct pointer
    "mouse_move",
    "cursor_position",
    "move",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "left_click_drag",
    "scroll",
    # keyboard
    "type",
    "key",
    "hold_key",
    # semantic, but still emit real input
    "click_element",
    "scroll_into_view",
    "file_dialog_paste",
    "solve_captcha",
}

# Actions that read the desktop or the shell without emitting input.
_READONLY_ACTION_NAMES = {
    "wait",
    "screenshot",
    "screenshot_to_clipboard",
    "focus_window",
    "focus_monitor",
    "run_shell",
}


class GuardedSession:
    """Runs desktop actions, but only into the one authorized window.

    Every input action is bracketed by a focus check. Any deviation raises
    instead of emitting input.
    """

    def __init__(self, window: AuthorizedWindow, *, settle: float = 0.25) -> None:
        self.window = window
        self.settle = settle
        self.emitted = 0

    # -- verification -------------------------------------------------
    def assert_ready(self, phase: str = "before action") -> dict:
        """Raise unless the authorized window owns the foreground right now."""
        if not _READERS["exists"](self.window.hwnd):  # type: ignore[operator]
            raise FocusViolation(
                f"REFUSING TO TYPE ({phase}): authorized window vanished "
                f"({self.window.describe()})"
            )

        snap = foreground_snapshot()
        reasons = evaluate_denylist(snap["title"], snap["process"])
        if reasons:
            raise FocusViolation(
                f"REFUSING TO TYPE ({phase}): foreground {snap['title']!r} "
                f"process={snap['process']!r} is on a denylist "
                f"({','.join(reasons)}). Automation halted."
            )

        if snap["hwnd"] != self.window.hwnd:
            raise FocusViolation(
                f"REFUSING TO TYPE ({phase}): authorized window "
                f"{self.window.describe()} is not in front; foreground is "
                f"hwnd={snap['hwnd']} title={snap['title']!r} "
                f"process={snap['process']!r}. Automation halted."
            )

        if snap["pid"] != self.window.pid:
            raise FocusViolation(
                f"REFUSING TO TYPE ({phase}): foreground pid {snap['pid']} does "
                f"not match authorized pid {self.window.pid}."
            )
        return snap

    def prepare(self) -> GuardedSession:
        """Raise the authorized window and confirm it owns the foreground."""
        raise_window(self.window)
        self.assert_ready("preparing session")
        return self

    # -- the only path that may emit input ---------------------------
    def run_input(self, action_name: str, action: Callable, /, **params) -> str:
        """Run one input action, but only if the authorized window is in front.

        Order matters. The pre-check is immediately adjacent to the call so the
        window in which focus can change is as small as Windows allows. The
        post-check turns a mid-action focus steal into a loud, reportable event
        rather than silent misdelivery.
        """
        if action_name not in _INPUT_ACTION_NAMES:
            raise ValueError(
                f"{action_name!r} is not an input action; use run_readonly() "
                f"for anything that does not move the mouse or keyboard"
            )

        self.assert_ready(f"before {action_name}")

        if not _INPUT_ENABLED:
            raise FocusViolation(
                f"SIMULATION: input is disabled, refused to emit {action_name!r}. "
                f"Guard checks passed for {self.window.describe()}."
            )

        result = action(**params)
        self.emitted += 1

        time.sleep(self.settle)
        after = self.assert_ready(f"after {action_name}")
        return f"{result} [fg={after['title']!r} pid={after['pid']}]"

    def run_readonly(self, action_name: str, action: Callable, /, **params) -> str:
        """Run a non-input action (focus_window, screenshot, run_shell...).

        Still verified against the authorized window, but no input check is
        needed because nothing is typed.
        """
        self.assert_ready(f"before {action_name}")
        return action(**params)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def describe_environment() -> str:
    """Read-only report of what the guard sees right now."""
    snap = foreground_snapshot()
    reasons = evaluate_denylist(snap["title"], snap["process"])
    verdict = ",".join(reasons) if reasons else "clear"
    return (
        f"foreground hwnd={snap['hwnd']} pid={snap['pid']} "
        f"title={snap['title']!r} process={snap['process']!r} verdict={verdict}"
    )


if __name__ == "__main__":
    print(describe_environment())
    print(f"input_enabled={_INPUT_ENABLED} python={sys.version.split()[0]}")