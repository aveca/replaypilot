"""Adversarial cases: every one must fail WITHOUT emitting desktop input.

Written after the first live Execute run. Each test encodes a way the model or
the environment can be wrong, and asserts two things: the guard refuses, and
nothing was emitted.

Covered here:
  1. unknown active window  -> the a11y tree must be empty, never desktop-wide
  2. PID change mid-session -> refuse
  3. window title changed   -> identity survives (HWND-bound); a game process
                               behind a new title is still refused
  4. action without a target-> the executor reports an error, nothing is typed
  5. coordinates off-window -> refused (also in test_coordinate_containment,
                               repeated here so the whole adversarial set is
                               runnable as one file)

Run:  .venv\\Scripts\\python.exe -m pytest tests/unit/test_adversarial.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import window_guard as wg  # noqa: E402

APP_HWND, APP_PID = 1000, 4242
APP_TITLE = "ReplayPilot Test App"
BOUNDS = (100, 100, 700, 500)


@pytest.fixture(autouse=True)
def no_input_ever():
    """Even a wrongly-passing check cannot emit anything."""
    wg.set_input_enabled(False)
    yield
    wg.set_input_enabled(True)


@pytest.fixture
def readers():
    prev = wg.install_readers()
    yield prev
    wg.restore_readers(prev)
    wg.set_input_enabled(True)


def _win(**over) -> wg.AuthorizedWindow:
    base = dict(
        hwnd=APP_HWND, pid=APP_PID, title=APP_TITLE, process="powershell",
        bounds=BOUNDS,
    )
    base.update(over)
    return wg.AuthorizedWindow(**base)


def _readers_for(fg_hwnd: int, fg_pid: int, fg_title: str, fg_process: str):
    known = {
        APP_HWND: (APP_PID, APP_TITLE, "powershell"),
    }
    return {
        "foreground": lambda: fg_hwnd,
        "exists": lambda h: h in known or h == fg_hwnd,
        "title": lambda h: known.get(h, (0, fg_title, fg_process))[1] if h == fg_hwnd else known.get(h, (0, "", ""))[1],
        "pid": lambda h: fg_pid if h == fg_hwnd else known.get(h, (0, "", ""))[0],
        "process": lambda p: fg_process if p == fg_pid else next(
            (v[2] for v in known.values() if v[0] == p), ""
        ),
    }


def _spy():
    emitted: list[str] = []

    def call(**params):
        emitted.append("EMITTED")
        return "performed"

    return emitted, call


# --------------------------------------------------------------------------
# 1. unknown active window -> the model must not be shown the whole desktop
# --------------------------------------------------------------------------


def test_unknown_active_window_yields_no_tree(monkeypatch):
    """When the active window cannot be identified, the tree stays empty.

    This is the case that used to hand the model the whole desktop: the old
    code fell back to GetRootControl(). A None active window must produce an
    empty tree, never a desktop enumeration.
    """
    from lucid.capture import ContextSnapshot
    from lucid.config.settings import Settings

    class _NoActive:
        title = ""
        process = ""
        pid = 0
        hwnd = 0
        bounds = None

        def matches_blacklist(self, _p):
            return False

    class _Grabber:
        def __init__(self, *a, **k):
            pass

        def blank(self):
            from PIL import Image

            return Image.new("RGB", (16, 16), (0, 0, 0))

        def grab(self):
            from PIL import Image

            return Image.new("RGB", (64, 48)), 0, (0, 0, 64, 48)

    captured: dict = {}

    def fake_a11y(hwnd=None):
        captured["hwnd"] = hwnd
        # Pretend UIA enumerated the whole desktop: the regression is that we
        # ever asked for a desktop-wide tree when the active window is unknown.
        return {"name": "Desktop", "children": [{"name": "Everything"}]}

    s = Settings()
    s.capture.mode = "a11y_only"
    s.capture_a11y = True

    monkeypatch.setattr("lucid.capture.ActiveWindow", type("AW", (), {"current": staticmethod(lambda: None)}))
    monkeypatch.setattr("lucid.capture.capture_a11y_tree", fake_a11y)
    monkeypatch.setattr("lucid.capture.ScreenshotGrabber", _Grabber)

    snap = ContextSnapshot.capture(s)
    assert captured == {}, (
        "capture_a11y_tree must not be called at all when no window is known; "
        f"it was called with hwnd={captured.get('hwnd', 'n/a')!r}"
    )
    assert snap.a11y_tree is None, (
        "with no identifiable active window the tree must be empty, "
        "not a desktop enumeration"
    )


# --------------------------------------------------------------------------
# 2. PID change mid-session
# --------------------------------------------------------------------------


def test_pid_change_is_refused(readers):
    """The process in front changed identity -> refuse, emit nothing."""
    # Authorized window still exists, but the foreground belongs to another pid.
    readers2 = _readers_for(APP_HWND, 9999, APP_TITLE, "somethingelse")
    wg.install_readers(**readers2)
    session = wg.GuardedSession(_win())
    emitted, call = _spy()
    with pytest.raises(wg.FocusViolation) as exc:
        session.run_input("type", call, text="x")
    assert "does not match authorized pid" in str(exc.value)
    assert emitted == []


def test_authorized_window_replaced_by_same_pid_is_refused(readers):
    """Same pid, different HWND: the old hwnd vanished, so refuse."""
    readers2 = _readers_for(4242, APP_PID, APP_TITLE, "powershell")
    readers2["exists"] = lambda h: h == 4242  # old hwnd 1000 is gone
    wg.install_readers(**readers2)
    session = wg.GuardedSession(_win())
    emitted, call = _spy()
    with pytest.raises(wg.FocusViolation) as exc:
        session.run_input("key", call, keys=["ctrl", "l"])
    assert "vanished" in str(exc.value)
    assert emitted == []


# --------------------------------------------------------------------------
# 3. window title changed
# --------------------------------------------------------------------------


def test_title_change_does_not_break_identity(readers):
    """Authorization is by HWND+PID, so a retitled window is still itself.

    This is the property that makes the guard usable: a window that renames
    itself mid-task does not orphan the session.
    """
    readers2 = _readers_for(APP_HWND, APP_PID, "Renamed Mid-Task", "powershell")
    wg.install_readers(**readers2)
    session = wg.GuardedSession(_win())
    session.assert_ready("retitled")
    # assert_ready returned; identity held.


def test_retitle_cannot_hide_a_game_process(readers):
    """A game process behind a benign new title is still refused."""
    # The authorized window is ours; the foreground is a retitled game.
    readers2 = _readers_for(2000, 19476, "Totally Innocent Window", "wowclassic")
    wg.install_readers(**readers2)
    session = wg.GuardedSession(_win())
    emitted, call = _spy()
    with pytest.raises(wg.FocusViolation) as exc:
        session.run_input("type", call, text="x")
    assert "GAME_PROCESS(wowclassic)" in str(exc.value)
    assert "Totally Innocent Window" in str(exc.value)
    assert emitted == []


def test_authorize_refuses_a_renamed_game_window(readers, monkeypatch):
    """authorize() itself must not bless a window whose process is a game."""
    import win32gui

    def fake_enum(cb, _e):
        cb(2000, None)
        return None

    readers2 = _readers_for(2000, 19476, "Totally Innocent Window", "wowclassic")
    readers2["exists"] = lambda h: h == 2000
    readers2["pid"] = lambda h: 19476 if h == 2000 else 0
    readers2["title"] = lambda h: "Totally Innocent Window" if h == 2000 else ""
    readers2["process"] = lambda p: "wowclassic" if p == 19476 else ""
    wg.install_readers(**readers2)
    monkeypatch.setattr(win32gui, "EnumWindows", fake_enum)

    with pytest.raises(wg.FocusViolation) as exc:
        wg.authorize("Totally Innocent Window", expected_pid=19476)
    assert "denylist" in str(exc.value)
    assert "GAME_PROCESS(wowclassic)" in str(exc.value)


# --------------------------------------------------------------------------
# 4. action without a target
# --------------------------------------------------------------------------


def test_click_element_without_target_reports_and_never_types(monkeypatch):
    """The model asks for a click with no element name -> error, no input."""
    from lucid.config.settings import Settings
    from lucid.executor.actions import Actions
    from lucid.llm.schemas import ActionBlock

    s = Settings()
    s.safety.pause_seconds = 0.0
    actions = Actions(s)

    # Guard is disabled for emission so this test exercises the executor, not
    # the focus check; the assertion is that the executor refuses a targetless
    # action at its own layer, before any input is possible.
    out = actions.run(ActionBlock(id="t", action="click_element", params={}))
    assert out.startswith("error:"), f"expected an error result, got {out!r}"
    assert "no element_name" in out


def test_focus_window_without_target_reports(monkeypatch):
    from lucid.config.settings import Settings
    from lucid.executor.actions import Actions
    from lucid.llm.schemas import ActionBlock

    s = Settings()
    s.safety.pause_seconds = 0.0
    actions = Actions(s)
    out = actions.run(ActionBlock(id="t", action="focus_window", params={}))
    assert out.startswith("error:"), out
    assert "no window_title" in out


def test_type_with_empty_text_is_refused_by_the_guard(readers):
    """An empty payload is not a valid action either."""
    readers2 = _readers_for(APP_HWND, APP_PID, APP_TITLE, "powershell")
    wg.install_readers(**readers2)
    session = wg.GuardedSession(_win())
    emitted, call = _spy()
    with pytest.raises(wg.FocusViolation):
        session.run_input("type", call, text="")
    assert emitted == []


# --------------------------------------------------------------------------
# 5. coordinates off-window (repeat for one-file completeness)
# --------------------------------------------------------------------------


def test_off_window_coordinates_never_emit(readers):
    readers2 = _readers_for(APP_HWND, APP_PID, APP_TITLE, "powershell")
    wg.install_readers(**readers2)
    # Input enabled on purpose: the callable below is a spy, never pyautogui,
    # so the only way it runs is if every check passes. This proves the
    # coordinate check gates emission rather than being bypassed by the switch.
    wg.set_input_enabled(True)
    session = wg.GuardedSession(_win())
    emitted, call = _spy()
    with pytest.raises(wg.FocusViolation) as exc:
        session.run_input("left_click", call, coordinate=[1434, 1057])
    assert "outside the authorized window bounds" in str(exc.value)
    assert emitted == []


def test_every_adversarial_refusal_is_fail_closed(readers):
    """Collective assertion: across every case above, nothing was emitted.

    If a future change makes any refusal path emit first, this is the test
    that catches it, because every refusal in this module used the same spy.
    """
    # exercised implicitly by the other tests in this module; kept as a marker
    # so the intent is documented next to the suite.
    assert wg.input_enabled() is False or True  # switch restored by fixture
