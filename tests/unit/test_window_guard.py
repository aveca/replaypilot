"""Guard tests that emit no input whatsoever.

Every test here drives `scripts.window_guard.py` through synthetic window
state via `install_readers`, and the module's global input switch is turned
OFF for the whole module. Even if a check were wrongly satisfied, no
keystroke, click or command could be produced.

The machine these run on has a real World of Warcraft client running, which
is exactly why the guard exists. See
docs/incidents/2026-10-17-foreground-hijack.md.

Run:  .venv\\Scripts\\python.exe -m pytest tests/unit/test_window_guard.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import window_guard as wg  # noqa: E402

APP_TITLE = "ReplayPilot Test App"
APP_HWND = 1000
APP_PID = 4242
WOW_HWND = 2000
WOW_PID = 19476


def _fake_readers(fg_hwnd: int, fg_pid: int, fg_title: str, fg_process: str, alive=...):
    """Readers describing a synthetic foreground plus one authorized window."""
    alive_set = {APP_HWND, WOW_HWND} if alive is ... else set(alive)
    known = {
        APP_HWND: (APP_PID, APP_TITLE, "powershell"),
        WOW_HWND: (WOW_PID, fg_title, fg_process),
    }
    return {
        "foreground": lambda: fg_hwnd,
        "exists": lambda h: h in alive_set,
        "title": lambda h: known.get(h, (0, "", ""))[1],
        "pid": lambda h: known.get(h, (0, "", ""))[0],
        "process": lambda p: next(
            (v[2] for v in known.values() if v[0] == p), ""
        ),
    }


@pytest.fixture(autouse=True)
def no_input_ever():
    """Belt and braces: make emission impossible for the whole module."""
    wg.set_input_enabled(False)
    yield
    wg.set_input_enabled(True)


@pytest.fixture
def restore():
    previous = wg.install_readers()
    yield previous
    wg.restore_readers(previous)
    wg.set_input_enabled(True)


def _authorized() -> wg.AuthorizedWindow:
    return wg.AuthorizedWindow(
        hwnd=APP_HWND, pid=APP_PID, title=APP_TITLE, process="powershell"
    )


# --------------------------------------------------------------------------
# Denylist detection
# --------------------------------------------------------------------------


def test_world_of_warcraft_title_is_denied(restore):
    wg.install_readers(**_fake_readers(WOW_HWND, WOW_PID, "World of Warcraft", "wowclassic"))
    reasons = wg.evaluate_denylist("World of Warcraft", "wowclassic")
    assert "GAME_TITLE(world of warcraft)" in reasons
    assert "GAME_PROCESS(wowclassic)" in reasons


def test_wow_denied_by_process_even_when_title_is_innocent(restore):
    """The hardening that the original guard lacked.

    A renamed, retitled or titleless game window must still be refused, because
    the block is keyed on the process name too.
    """
    reasons = wg.evaluate_denylist("Some Benign Window", "wowclassic")
    assert any(r.startswith("GAME_PROCESS") for r in reasons), reasons


def test_wow_denied_when_title_is_empty(restore):
    reasons = wg.evaluate_denylist("", "wowclassic")
    assert any(r.startswith("GAME_PROCESS") for r in reasons), reasons


def test_credential_windows_are_denied(restore):
    assert any(r.startswith("SECRET") for r in wg.evaluate_denylist("Bitwarden", ""))
    assert any(
        r.startswith("SECRET")
        for r in wg.evaluate_denylist("", "credentialuiproviderhost")
    )


def test_ordinary_window_is_allowed(restore):
    assert wg.evaluate_denylist(APP_TITLE, "powershell") == []


# --------------------------------------------------------------------------
# Authorization refuses before anything is typed
# --------------------------------------------------------------------------


def test_session_refuses_when_game_is_foreground(restore):
    wg.install_readers(**_fake_readers(WOW_HWND, WOW_PID, "World of Warcraft", "wowclassic"))
    session = wg.GuardedSession(_authorized())
    with pytest.raises(wg.FocusViolation) as exc:
        session.assert_ready()
    assert "denylist" in str(exc.value)
    assert "wowclassic" in str(exc.value)


def test_session_refuses_when_other_window_is_foreground(restore):
    wg.install_readers(**_fake_readers(WOW_HWND, WOW_PID, "Notepad", "notepad"))
    session = wg.GuardedSession(_authorized())
    with pytest.raises(wg.FocusViolation) as exc:
        session.assert_ready()
    assert "not in front" in str(exc.value)


def test_session_refuses_when_authorized_window_vanished(restore):
    wg.install_readers(**_fake_readers(APP_HWND, APP_PID, APP_TITLE, "powershell", alive={WOW_HWND}))
    session = wg.GuardedSession(_authorized())
    with pytest.raises(wg.FocusViolation) as exc:
        session.assert_ready()
    assert "vanished" in str(exc.value)


def test_session_allows_only_the_exact_authorized_hwnd(restore):
    """A different window with the same title must not be accepted."""
    readers = _fake_readers(3000, 5555, APP_TITLE, "powershell")
    readers["exists"] = lambda h: h in {APP_HWND, WOW_HWND, 3000}
    readers["pid"] = lambda h: {APP_HWND: APP_PID, 3000: 5555, WOW_HWND: WOW_PID}.get(h, 0)
    readers["process"] = lambda p: {APP_PID: "powershell", 5555: "powershell", WOW_PID: "wowclassic"}.get(p, "")
    wg.install_readers(**readers)
    session = wg.GuardedSession(_authorized())
    with pytest.raises(wg.FocusViolation) as exc:
        session.assert_ready()
    assert "not in front" in str(exc.value)


def test_session_accepts_the_authorized_window(restore):
    wg.install_readers(**_fake_readers(APP_HWND, APP_PID, APP_TITLE, "powershell"))
    session = wg.GuardedSession(_authorized())
    snap = session.assert_ready()
    assert snap["hwnd"] == APP_HWND
    assert snap["pid"] == APP_PID


# --------------------------------------------------------------------------
# Nothing is emitted, ever
# --------------------------------------------------------------------------


def test_run_input_emits_nothing_when_checks_pass_but_input_disabled(restore):
    """Checks pass, yet nothing is typed. This is the whole point."""
    wg.install_readers(**_fake_readers(APP_HWND, APP_PID, APP_TITLE, "powershell"))
    emitted: list[str] = []

    def spy(**params):
        emitted.append("typed")
        return "typed"

    session = wg.GuardedSession(_authorized())
    with pytest.raises(wg.FocusViolation) as exc:
        session.run_input("type", spy, text="hello")
    assert "SIMULATION" in str(exc.value)
    assert emitted == [], "input escaped despite the simulation switch"


def test_run_input_refuses_for_a_game_foreground_without_calling_the_action(restore):
    wg.install_readers(**_fake_readers(WOW_HWND, WOW_PID, "World of Warcraft", "wowclassic"))
    emitted: list[str] = []

    def spy(**params):
        emitted.append("typed")
        return "typed"

    session = wg.GuardedSession(_authorized())
    with pytest.raises(wg.FocusViolation):
        session.run_input("key", spy, keys=["ctrl", "a"])
    assert emitted == [], "action ran while a game window was in front"


def test_run_input_rejects_non_input_actions(restore):
    wg.install_readers(**_fake_readers(APP_HWND, APP_PID, APP_TITLE, "powershell"))
    session = wg.GuardedSession(_authorized())
    with pytest.raises(ValueError) as exc:
        session.run_input("run_shell", lambda **p: "x", cmd="dir")
    assert "not an input action" in str(exc.value)


def test_every_input_action_name_is_classified(restore):
    for name in ("type", "key", "left_click", "scroll", "triple_click", "hold_key"):
        assert name in wg._INPUT_ACTION_NAMES


# --------------------------------------------------------------------------
# No fallback to the active window
# --------------------------------------------------------------------------


def test_raise_window_refuses_instead_of_falling_back(restore, monkeypatch):
    """If the authorized window cannot be raised, the guard must raise.

    There is no code path that types into the window that happens to be active.
    """
    readers = _fake_readers(WOW_HWND, WOW_PID, "World of Warcraft", "wowclassic")
    wg.install_readers(**readers)

    import win32con
    import win32gui
    import win32process

    monkeypatch.setattr(win32gui, "IsIconic", lambda h: False)
    monkeypatch.setattr(win32gui, "ShowWindow", lambda h, c: None)
    monkeypatch.setattr(win32gui, "BringWindowToTop", lambda h: None)
    monkeypatch.setattr(win32gui, "SetForegroundWindow", lambda h: None)
    monkeypatch.setattr(win32gui, "SetActiveWindow", lambda h: None)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: WOW_HWND)
    monkeypatch.setattr(win32con, "SW_RESTORE", 9)
    monkeypatch.setattr(win32con, "SW_SHOW", 5)
    monkeypatch.setattr(win32process, "AttachThreadInput", lambda a, b, c: None)
    monkeypatch.setattr(win32process, "GetWindowThreadProcessId", lambda h: (WOW_PID, WOW_PID))
    import win32api

    monkeypatch.setattr(win32api, "GetCurrentThreadId", lambda: WOW_PID)
    monkeypatch.setattr(wg.time, "sleep", lambda _s: None)

    with pytest.raises(wg.FocusViolation) as exc:
        wg.raise_window(_authorized(), attempts=2)
    msg = str(exc.value)
    assert "REFUSING" in msg
    assert "Not falling back to the active window" in msg


def test_authorize_requires_explicit_pid(restore):
    with pytest.raises(wg.FocusViolation) as exc:
        wg.authorize(APP_TITLE, expected_pid=0)
    assert "expected_pid" in str(exc.value)


def test_authorize_refuses_window_of_another_process(restore, monkeypatch):
    import win32gui

    readers = _fake_readers(APP_HWND, APP_PID, APP_TITLE, "powershell")
    wg.install_readers(**readers)
    monkeypatch.setattr(
        win32gui, "EnumWindows", lambda cb, _e: [cb(APP_HWND, None), True] and None
    )
    with pytest.raises(wg.FocusViolation) as exc:
        wg.authorize(APP_TITLE, expected_pid=999999)
    assert "another process" in str(exc.value)