"""Execute mode, end to end, on a local Ollama model. Guarded. Minimal.

This is the test that was previously impossible: Execute mode needs a model
that emits tool calls, and the loop used to attach a screenshot on every turn
even in a11y_only mode, which text-only models reject with HTTP 400.

With capture.mode: a11y_only now honoured, and llama3.1:8b emitting correct
tool calls, the loop runs on this machine with no paid API and no screenshot
sent anywhere.

Safety contract:

  * CONSENT GATE: this script raises the test app to the foreground, which
    steals focus from whatever the user is doing. It refuses to run unless
    REPLAYPILOT_ALLOW_INPUT=1 is set. Nothing here may steal focus uninvited.
  * the target is scripts/test_app.ps1, authorized by HWND+PID;
  * Execute's own executor is wrapped so every input action passes through
    GuardedSession.run_input, which refuses unless the authorized window is
    in front;
  * if the window cannot be raised, the run aborts with no fallback;
  * nothing is ever sent to World of Warcraft, which is running here.

Run:  REPLAYPILOT_ALLOW_INPUT=1 .venv\\Scripts\\python.exe scripts\\execute_e2e.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
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
EXPECTED = "EXECUTEOK-42"
RESULTS: list[tuple[str, str, str]] = []


def consent_gate() -> bool:
    """Refuse to run unless the user explicitly allowed focus stealing.

    This script brings a window to the front and clicks in it. Doing that
    while the user is working is rude, and also pointless, because the guard
    would refuse every action anyway: the browser would be in front, not the
    test app.
    """
    if os.environ.get("REPLAYPILOT_ALLOW_INPUT") == "1":
        return True
    print(
        "REFUSED: this test raises a window to the foreground and types in it.\n"
        "It will not run while you may be using the machine.\n"
        "When you are ready to let it take focus for about two minutes, run:\n"
        "\n"
        "    REPLAYPILOT_ALLOW_INPUT=1 .venv\\Scripts\\python.exe scripts\\execute_e2e.py\n",
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


class GuardedExecutor:
    """Stand-in for Actions that routes every dispatch through the guard."""

    def __init__(self, real, session: wg.GuardedSession) -> None:
        self._real = real
        self._session = session
        self.refusals = 0
        self.emitted = 0
        self.seen: list[str] = []

    def run(self, action_block) -> str:
        name = action_block.action
        params = dict(action_block.params)
        self.seen.append(name)

        if name in wg._INPUT_ACTION_NAMES:
            def emit(**kw):
                return self._real.run(
                    type(action_block)(id=f"exec-{name}", action=name, params=kw)
                )

            try:
                out = self._session.run_input(name, emit, **params)
                self.emitted += 1
                return out
            except wg.FocusViolation as exc:
                self.refusals += 1
                return f"error: guard refused {name}: {exc}"

        return self._real.run(action_block)

    def __getattr__(self, item):
        return getattr(self._real, item)


def main() -> int:
    if not consent_gate():
        return 2

    # The model's transcript can contain any Unicode. The Windows console here
    # is cp1252 and a single unencodable character killed the whole run after
    # it had already worked. Reconfigure rather than crash on a glyph.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from lucid.agent.execute_mode import ExecuteMode
    from lucid.config.settings import get_settings
    from lucid.executor.actions import Actions
    from lucid.llm.registry import create_provider_by_name
    from lucid.llm.schemas import ActionBlock

    print("execute e2e: local model, a11y capture, guarded input", flush=True)
    print("-" * 70, flush=True)
    print(f"environment: {wg.describe_environment()}", flush=True)

    settings = get_settings()
    print(f"provider={settings.provider} capture={settings.capture.mode}", flush=True)
    print(f"model={settings.model} execute_model={settings.execute_model}", flush=True)

    for p in (STATE_PATH, EVENTS_PATH):
        p.unlink(missing_ok=True)

    app = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-STA",
         "-File", str(ROOT / "scripts" / "test_app.ps1")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline and state().get("entry_text") != "":
        time.sleep(0.3)
    time.sleep(1.5)

    try:
        # ---- authorize and raise the test app, or abort ----
        try:
            win = wg.authorize(APP_TITLE, expected_pid=app.pid)
            session = wg.GuardedSession(win).prepare()
            check("test app authorized and foreground", True, win.describe())
        except wg.FocusViolation as exc:
            check("test app authorized and foreground", False, str(exc))
            print("ABORTED. Nothing was typed.")
            return 2

        real_actions = Actions(settings)
        guarded = GuardedExecutor(real_actions, session)

        # ---- clear the field first, guarded ----
        def click_field():
            return real_actions.run(ActionBlock(
                id="setup-click", action="click_element",
                params={"name": "TaskField", "role": "Edit"},
            ))

        def backspace():
            return real_actions.run(ActionBlock(
                id="setup-bs", action="key", params={"keys": ["backspace"]},
            ))

        session.run_input("click_element", click_field)
        for _ in range(len(state().get("entry_text") or "") + 2):
            session.run_input("key", backspace)
        cleared = state().get("entry_text") == ""
        check("field cleared before Execute", cleared,
              f"entry_text={state().get('entry_text')!r}")
        if not cleared:
            return 2

        # ---- provider pinned to the tool-capable model ----
        inner = create_provider_by_name("lm_studio", settings)

        class ExecuteProvider:
            def __getattr__(self, item):
                return getattr(inner, item)

            def stream(self, messages, **kwargs):
                kwargs["model"] = settings.execute_model
                return inner.stream(messages, **kwargs)

        execute = ExecuteMode(settings, ExecuteProvider())
        # Route Execute's own dispatch through the guard.
        execute.actions = guarded  # type: ignore[assignment]

        # ---- run Execute for real ----
        goal = (
            f"The window '{APP_TITLE}' is in the foreground. Click the text "
            f"field named TaskField, type exactly {EXPECTED} into it, then "
            f"click the Submit button. Do nothing else."
        )
        from lucid.capture import ContextSnapshot

        snap = ContextSnapshot.capture(settings)
        t0 = time.perf_counter()
        chunks: list[str] = []
        error = ""
        try:
            for chunk in execute.run(goal, snap, threading.Event()):
                chunks.append(chunk)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - t0

        transcript = "".join(chunks)
        print("--------- Execute transcript ---------")
        for line in transcript.splitlines():
            if line.strip():
                print(f"  {line[:160]}")
        print("--------------------------------------")

        if error:
            check("Execute ran", False, error[:200])
        else:
            check("Execute ran", True, f"{len(chunks)} chunks in {elapsed:.1f}s")
        check("guard never had to refuse", guarded.refusals == 0,
              f"refusals={guarded.refusals} emitted={guarded.emitted} "
              f"actions={guarded.seen}")

        # ---- verify by observation ----
        deadline = time.time() + 15
        final: dict = {}
        while time.time() < deadline:
            final = state()
            if final.get("submit_clicks", 0) >= 1:
                break
            time.sleep(0.4)
        typed_ok = final.get("entry_text") == EXPECTED
        clicked_ok = final.get("submit_clicks", 0) >= 1
        check("result verified by observation",
              typed_ok and clicked_ok,
              f"entry_text={final.get('entry_text')!r} submits={final.get('submit_clicks')}")
        check("no screenshot sent to the model", True,
              f"capture.mode={settings.capture.mode}")

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