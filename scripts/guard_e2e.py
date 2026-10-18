"""End-to-end guard check against the isolated test app. Minimal real input.

This is the only place the hardened guard is exercised against a real desktop.
Everything it sends is bracketed by a (HWND, PID) focus check, so a keystroke
cannot reach any window other than the test app we launched.

Rules honoured here:

  * the authorized window is the test app, identified by the PID of the
    process this script started;
  * if that window cannot be brought to the foreground, the run aborts. There
    is no fallback to the active window;
  * nothing is sent while a game or credential window is in front;
  * the number of emitted actions is asserted at the end, so an unexpected
    emission fails the run.

Run:  .venv\\Scripts\\python.exe scripts\\guard_e2e.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
STATE_PATH = ROOT / "data" / "testapp_state.json"
EVENTS_PATH = ROOT / "data" / "testapp_events.log"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import window_guard as wg  # noqa: E402

APP_TITLE = "ReplayPilot Test App"
RESULTS: list[tuple[str, str, str]] = []


def consent_gate() -> bool:
    """Refuse to run unless the user explicitly allowed focus stealing."""
    if os.environ.get("REPLAYPILOT_ALLOW_INPUT") == "1":
        return True
    print(
        "REFUSED: this test raises a window to the foreground and clicks in it.\n"
        "When you are ready to let it take focus for about a minute, run:\n"
        "\n"
        "    REPLAYPILOT_ALLOW_INPUT=1 .venv\\Scripts\\python.exe scripts\\guard_e2e.py\n",
        flush=True,
    )
    return False


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    RESULTS.append((name, status, detail))
    print(f"[{status:4}] {name}" + (f" -- {detail}" if detail else ""))


def state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}


def main() -> int:
    if not consent_gate():
        return 2

    from lucid.config.settings import get_settings
    from lucid.executor.actions import Actions
    from lucid.llm.schemas import ActionBlock

    print("guard e2e: isolated test app only", flush=True)
    print("-" * 70)
    print(f"environment before launch: {wg.describe_environment()}")

    for p in (STATE_PATH, EVENTS_PATH):
        p.unlink(missing_ok=True)

    app = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-STA",
         "-File", str(ROOT / "scripts" / "test_app.ps1")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"test app pid={app.pid}")

    deadline = time.time() + 20
    while time.time() < deadline and state().get("entry_text") != "":
        time.sleep(0.3)
    time.sleep(1.5)

    try:
        # ---- 1. authorize exactly the window we launched ----
        try:
            win = wg.authorize(APP_TITLE, expected_pid=app.pid)
            check("authorize the test app by HWND+PID", True, win.describe())
        except wg.FocusViolation as exc:
            check("authorize the test app by HWND+PID", False, str(exc))
            return 2

        # ---- 2. bring it forward, or abort with no fallback ----
        try:
            session = wg.GuardedSession(win).prepare()
            check("raise authorized window, no fallback", True,
                  f"foreground is the test app (pid={app.pid})")
        except wg.FocusViolation as exc:
            # Fail-closed: we stop. We do NOT type into whatever is in front.
            check("raise authorized window, no fallback", False, str(exc))
            print("ABORTED. Nothing was typed. This is the designed behaviour.")
            return 2

        actions = Actions(get_settings())

        def click_task_field() -> str:
            return actions.run(ActionBlock(
                id="guard-e2e-click", action="click_element",
                params={"name": "TaskField", "role": "Edit"},
            ))

        def type_marker() -> str:
            return actions.run(ActionBlock(
                id="guard-e2e-type", action="type",
                params={"text": "GUARDOK-1", "use_clipboard": True},
            ))

        # ---- 3. one guarded input action ----
        before = session.emitted
        out = session.run_input("click_element", click_task_field)
        check("guarded click_element emitted", session.emitted == before + 1,
              out)

        # ---- 4. one guarded input action that types ----
        before = session.emitted
        out = session.run_input("type", type_marker)
        check("guarded type emitted", session.emitted == before + 1, out)

        # ---- 5. the app really received it ----
        got = state().get("entry_text")
        check("test app received the guarded input", got == "GUARDOK-1",
              f"entry_text={got!r}")

        # ---- 6. a non-input action needs no input switch ----
        out = session.run_readonly(
            "focus_window",
            lambda **kw: actions.run(ActionBlock(
                id="guard-e2e-focus", action="focus_window",
                params={"window_title": APP_TITLE})),
        )
        check("non-input action allowed under the guard", True, out)

        # ---- 7. refuse if the game is in front (simulation, zero input) ----
        previous = wg.install_readers(
            foreground=lambda: win.hwnd + 1,
            exists=lambda h: True,
            title=lambda h: "World of Warcraft" if h != win.hwnd else APP_TITLE,
            pid=lambda h: 19476 if h != win.hwnd else win.pid,
            process=lambda p: "wowclassic" if p == 19476 else "powershell",
        )
        try:
            refused = False
            try:
                session.run_input("type", type_marker)
            except wg.FocusViolation:
                refused = True
            check("refuses a game window claiming the foreground", refused,
                  "guard aborted before emitting")
        finally:
            wg.restore_readers(previous)

        # ---- 8. exact emission count ----
        check("exactly 2 input actions were emitted", session.emitted == 2,
              f"emitted={session.emitted}")
        print(f"environment after run: {wg.describe_environment()}")

    finally:
        app.terminate()
        try:
            app.wait(timeout=5)
        except subprocess.TimeoutExpired:
            app.kill()

    print("-" * 70)
    passed = sum(1 for _, s, _ in RESULTS if s == "PASS")
    print(f"{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 2


if __name__ == "__main__":
    raise SystemExit(main())