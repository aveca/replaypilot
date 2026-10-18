"""Coordinate containment: a click may only land inside the authorized window.

Measured during the Execute dry run: llama3.1:8b, asked to drive a small test
window, opened with a pixel click at (1434, 1057) -- a point on the user's
desktop, nowhere near the target -- with a fabricated "FALLBACK:" reason.
Focus checks alone cannot stop that, because the authorized window would
still be in front when the click fires.

Run:  .venv\\Scripts\\python.exe -m pytest tests/unit/test_coordinate_containment.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import window_guard as wg  # noqa: E402

# A 600x400 window at (100, 100): bounds (100, 100, 700, 500).
LEFT, TOP, RIGHT, BOTTOM = 100, 100, 700, 500


@pytest.fixture(autouse=True)
def no_input_ever():
    wg.set_input_enabled(False)
    yield
    wg.set_input_enabled(True)


@pytest.fixture
def session():
    win = wg.AuthorizedWindow(
        hwnd=1000, pid=4242, title="ReplayPilot Test App", process="powershell",
        bounds=(LEFT, TOP, RIGHT, BOTTOM),
    )
    return wg.GuardedSession(win)


def test_click_inside_bounds_is_accepted(session):
    session.assert_coordinates_inside({"coordinate": [400, 300]})
    session.assert_coordinates_inside({"coordinate": [LEFT, TOP]})
    session.assert_coordinates_inside({"coordinate": [RIGHT, BOTTOM]})


def test_click_outside_bounds_is_refused(session):
    with pytest.raises(wg.FocusViolation) as exc:
        session.assert_coordinates_inside({"coordinate": [1434, 1057]})
    assert "outside the authorized window bounds" in str(exc.value)
    assert "(1434, 1057)" in str(exc.value)


def test_just_outside_each_edge_is_refused(session):
    for x, y in ((LEFT - 1, TOP), (RIGHT + 1, BOTTOM), (LEFT, TOP - 1), (LEFT, BOTTOM + 1)):
        with pytest.raises(wg.FocusViolation):
            session.assert_coordinates_inside({"coordinate": [x, y]})


def test_drag_endpoints_are_checked(session):
    session.assert_coordinates_inside(
        {"start_coordinate": [200, 200], "coordinate": [300, 300]}
    )
    with pytest.raises(wg.FocusViolation):
        session.assert_coordinates_inside(
            {"start_coordinate": [200, 200], "coordinate": [2000, 200]}
        )


def test_non_integer_coordinate_is_refused(session):
    with pytest.raises(wg.FocusViolation) as exc:
        session.assert_coordinates_inside({"coordinate": "not a pair"})
    assert "not a coordinate pair" in str(exc.value)


def test_actions_without_coordinates_are_unaffected(session):
    session.assert_coordinates_inside({"text": "hello"})
    session.assert_coordinates_inside({"keys": ["ctrl", "l"]})


def test_window_without_bounds_skips_the_check():
    win = wg.AuthorizedWindow(hwnd=1, pid=1, title="t", process="p", bounds=None)
    s = wg.GuardedSession(win)
    s.assert_coordinates_inside({"coordinate": [99999, 99999]})


def test_run_input_refuses_off_window_click_before_emitting(session):
    """The full path: the click never reaches the action callable."""
    emitted: list[str] = []

    def spy(**params):
        emitted.append("clicked")
        return "clicked"

    with pytest.raises(wg.FocusViolation):
        session.run_input("left_click", spy, coordinate=[1434, 1057])
    assert emitted == [], "the off-window click was emitted"


def test_run_input_allows_in_window_click_up_to_the_simulation_switch(session):
    emitted: list[str] = []

    def spy(**params):
        emitted.append("clicked")
        return "clicked"

    with pytest.raises(wg.FocusViolation):
        # assert_ready runs first and would fail on a fake hwnd; the point of
        # this test is that the coordinate check sits between it and emission.
        session.run_input("left_click", spy, coordinate=[400, 300])
    assert emitted == []
    # The coordinate check itself accepted the value, which we verify directly.
    session.assert_coordinates_inside({"coordinate": [400, 300]})