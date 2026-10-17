"""Diagnostic: why does ctrl+a / delete not clear the test field?

Uses scripts/window_guard.py so no keystroke can reach any window other than
the test app. Reports the app-side state after every sub-step.

Run:  .venv\\Scripts\\python.exe scripts\\diag_reset.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "testapp_state.json"
EVENTS_PATH = ROOT / "data" / "testapp_events.log"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import window_guard  # noqa: E402

APP_TITLE = "ReplayPilot Test App"


def state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}


def show(label: str) -> None:
    s = state()
    print(f"  {label:34} -> entry_text={s.get('entry_text')!r} submits={s.get('submit_clicks')}")


def main() -> int:
    from lucid.config.settings import get_settings
    from lucid.executor.actions import Actions
    from lucid.llm.schemas import ActionBlock

    actions = Actions(get_settings())

    def run(action: str, **params) -> str:
        return actions.run(ActionBlock(id=f"diag-{action}", action=action, params=params))

    for p in (STATE_PATH, EVENTS_PATH):
        p.unlink(missing_ok=True)

    app = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-STA",
         "-File", str(ROOT / "scripts" / "test_app.ps1")]
    )
    deadline = time.time() + 20
    while time.time() < deadline and state().get("entry_text") != "":
        time.sleep(0.3)
    time.sleep(2.0)
    print(f"app up. guard: {window_guard.describe_environment()}")

    try:
        # Seed the field through Lucid itself.
        window_guard.bring_forward_and_verify(APP_TITLE)
        run("type", text="SEEDVALUE-123")
        time.sleep(0.8)
        show("after type SEEDVALUE-123")

        # Attempt 1: click the field, ctrl+a, delete
        window_guard.bring_forward_and_verify(APP_TITLE)
        print(run("click_element", name="TaskField", role="Edit"))
        time.sleep(0.5)
        show("after click TaskField")
        print(run("key", keys=["ctrl", "a"]))
        time.sleep(0.6)
        show("after ctrl+a (no visible change ok)")
        print(run("key", keys=["delete"]))
        time.sleep(0.8)
        show("after delete")

        # Attempt 2: backspace x N
        for _ in range(14):
            run("key", keys=["backspace"])
            time.sleep(0.05)
        time.sleep(0.8)
        show("after 14x backspace")

        # Attempt 3: triple-click then type (replaces selection)
        window_guard.bring_forward_and_verify(APP_TITLE)
        print(run("click_element", name="TaskField", role="Edit", clicks=3))
        time.sleep(0.5)
        run("type", text="X")
        time.sleep(0.8)
        show("after triple-click + type X")
    finally:
        app.terminate()
        try:
            app.wait(timeout=5)
        except subprocess.TimeoutExpired:
            app.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())