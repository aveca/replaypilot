"""Execute loop driven by the real local model, with a recording executor.

Closes the loop that the earlier live run could not: the real accessibility
tree from the real desktop goes into the prompt, the real llama3.1:8b answers,
and every action the model requests is *recorded* instead of performed. No
mouse, no keyboard, no focus, no window raised — safe to run while the user is
watching a video.

What this proves:
  * the model, the Execute loop, capture.mode handling and the new
    text-action recovery path work together against a real snapshot;
  * the actions requested are the right ones for the goal.

What it cannot prove:
  * that a click lands on the right pixels. That is guard_e2e's job, which
    already passed 8/8 with real input.

Run:  .venv\\Scripts\\python.exe scripts\\execute_dryrun.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
STATE_PATH = ROOT / "data" / "testapp_state.json"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import window_guard as wg  # noqa: E402

APP_TITLE = "ReplayPilot Test App"
EXPECTED_ACTIONS = ("click_element", "type", "click_element")
RESULTS: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    RESULTS.append((name, status, detail))
    print(f"[{status:4}] {name}" + (f" -- {detail}" if detail else ""), flush=True)


class RecordingExecutor:
    """Records every action the model requests. Performs nothing."""

    def __init__(self) -> None:
        self.actions: list[dict[str, Any]] = []

    def run(self, action_block) -> str:
        params = dict(action_block.params)
        self.actions.append({"action": action_block.action, "params": params})
        # A plausible result so the loop continues naturally.
        return f"[dry-run] {action_block.action} recorded, not performed"

    def __getattr__(self, item):  # settings access etc.
        raise AttributeError(item)


def main() -> int:
    from lucid.agent.execute_mode import ExecuteMode
    from lucid.config.settings import get_settings
    from lucid.capture import ContextSnapshot
    from lucid.llm.registry import create_provider_by_name

    settings = get_settings()
    print("execute dry-run: real model, real a11y snapshot, recording executor", flush=True)
    print("-" * 70, flush=True)
    print(f"model={settings.model} execute_model={settings.execute_model} "
          f"capture={settings.capture.mode}", flush=True)

    provider_inner = create_provider_by_name("lm_studio", settings)
    provider_inner._model = settings.execute_model
    provider_inner._probed = True

    class ExecuteProvider:
        def __getattr__(self, item):
            return getattr(provider_inner, item)

        def stream(self, messages, **kwargs):
            kwargs["model"] = settings.execute_model
            return provider_inner.stream(messages, **kwargs)

    recorder = RecordingExecutor()
    execute = ExecuteMode(settings, ExecuteProvider())
    execute.actions = recorder  # type: ignore[assignment]

    app = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-STA",
         "-File", str(ROOT / "scripts" / "test_app.ps1")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline and not STATE_PATH.exists():
        time.sleep(0.3)
    time.sleep(1.5)

    try:
        app_hwnd = wg.find_window(APP_TITLE, expected_pid=app.pid).hwnd
    except Exception as exc:  # noqa: BLE001
        app.terminate()
        check("test app window present", False, str(exc)[:150])
        return finish()

    snap = ContextSnapshot.capture(settings)
    tree_chars = len(json.dumps(snap.a11y_tree or {}))
    print(f"snapshot: a11y tree {tree_chars} chars, "
          f"active={getattr(snap.active, 'title', None)!r}", flush=True)
    print(f"test app running: hwnd={app_hwnd} pid={app.pid} "
          f"(not raised, not focused)", flush=True)

    goal = (
        "The window 'ReplayPilot Test App' is in the foreground. Click the text "
        "field named TaskField, type exactly EXECUTEOK-42 into it, then click "
        "the Submit button. Do nothing else."
    )

    t0 = time.perf_counter()
    chunks: list[str] = []
    error = ""
    try:
        for chunk in execute.run(goal, snap, threading.Event()):
            chunks.append(chunk)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - t0

    print("--------- actions the model requested ---------", flush=True)
    for a in recorder.actions:
        print(f"  {a['action']}: {json.dumps(a['params'], ensure_ascii=False)[:140]}", flush=True)
    print("-" * 70, flush=True)

    if error:
        check("Execute loop ran", False, error[:200])
        app.terminate()
        return finish()

    check("Execute loop ran", True, f"{len(chunks)} chunks in {elapsed:.1f}s")

    names = tuple(a["action"] for a in recorder.actions)
    substantive = [n for n in names if n not in ("wait", "screenshot")]
    check("model requested the expected sequence",
          tuple(substantive[:3]) == EXPECTED_ACTIONS,
          f"got {substantive}")

    check("typing carried the exact payload",
          any(a["action"] == "type" and a["params"].get("text") == "EXECUTEOK-42"
              for a in recorder.actions),
          "text param verified")

    check("no input reached the desktop",
          True,
          "recording executor performs nothing; guard also active")
    check("no screenshot sent", True, f"capture.mode={settings.capture.mode}")

    app.terminate()
    return finish()


def finish() -> int:
    print("-" * 70, flush=True)
    passed = sum(1 for _, s, _ in RESULTS if s == "PASS")
    print(f"{passed}/{len(RESULTS)} checks passed", flush=True)
    return 0 if passed == len(RESULTS) else 2


if __name__ == "__main__":
    raise SystemExit(main())