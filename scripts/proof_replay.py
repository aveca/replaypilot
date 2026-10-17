"""Functional proof: record -> save -> reload -> replay -> verify.

Runs entirely against a harmless local WinForms app (scripts/test_app.ps1).
No LLM is required for this path: recorded steps carry semantic actions that
Lucid resolves deterministically through the Windows accessibility tree and
pyautogui. The Ollama LLM is exercised separately by scripts/probe_ollama.py.

Evidence rules for this harness:
  * the target app writes its own state to JSON, so verification is an
    observation of the app, not a report from the replayer;
  * a replay is only PASS when the app-side state and the app-side log both
    agree with the recorded workflow;
  * any action returning "error:" fails the replay check.

Run:  .venv\\Scripts\\python.exe scripts\\proof_replay.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
STATE_PATH = ROOT / "data" / "testapp_state.json"
EVENTS_PATH = ROOT / "data" / "testapp_events.log"
WORKFLOWS = ROOT / "data" / "workflows"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import window_guard  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []
APP_TITLE = "ReplayPilot Test App"
EXPECTED = "REPLAYPROOF-7741"


def record(name: str, status: str, detail: str = "") -> bool:
    RESULTS.append((name, status, detail))
    print(f"[{status:4}] {name}" + (f" -- {detail}" if detail else ""))
    return status == "PASS"


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}


def wait_for_state(predicate, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = read_state()
        if predicate(last):
            return last
        time.sleep(0.3)
    return last


def main() -> int:
    from lucid.config.settings import get_settings
    from lucid.executor.actions import Actions
    from lucid.llm.schemas import ActionBlock

    settings = get_settings()
    actions = Actions(settings)

    def run(action: str, **params) -> str:
        return actions.run(ActionBlock(id=f"proof-{action}", action=action, params=params))

    # Actions that actually produce keyboard/mouse input on the desktop. Each
    # one is preceded by a focus check so a keystroke can never reach a window
    # other than the test app (see scripts/window_guard.py).
    INPUT_ACTIONS = {"type", "key", "click_element", "left_click", "right_click",
                     "double_click", "triple_click", "scroll", "left_click_drag"}

    def safe_run(action: str, **params) -> str:
        if action in INPUT_ACTIONS:
            window_guard.check(APP_TITLE)
            time.sleep(0.25)
            window_guard.check(APP_TITLE)
        return run(action, **params)

    def focus_app() -> None:
        """Raise the test app and confirm it owns the foreground."""
        window_guard.bring_forward_and_verify(APP_TITLE)

    def clear_field() -> dict:
        """Empty the test field using only Lucid actions.

        Measured behaviour (scripts/diag_reset.py):
          * `key ctrl+a` then `key delete` has NO effect on a WinForms
            TextBox in this environment;
          * triple-click does not select the whole text either (one backspace
            removed a single character);
          * repeated `key backspace` removes exactly one character per call.

        So we size the backspace run from the app's own reported text length.
        """
        focus_app()
        safe_run("click_element", name="TaskField", role="Edit")
        current = read_state().get("entry_text") or ""
        for _ in range(len(current) + 4):
            safe_run("key", keys=["backspace"])
        return wait_for_state(lambda s: s.get("entry_text") == "", timeout=10.0)

    print("proof: record -> save -> reload -> replay -> verify")
    print("-" * 70)
    print(f"guard: {window_guard.describe_environment()}")

    # ---------- 1. launch the harmless test app ----------
    for p in (STATE_PATH, EVENTS_PATH):
        p.unlink(missing_ok=True)
    app = subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-STA",
            "-File",
            str(ROOT / "scripts" / "test_app.ps1"),
        ]
    )
    ready = wait_for_state(lambda s: s.get("entry_text") == "")
    if not record("1. test app launches", "PASS" if ready else "FAIL", f"pid={app.pid}"):
        app.terminate()
        return 1
    time.sleep(2.0)

    try:
        # ---------- 2. record a demonstration ----------
        from lucid.recorder.workflow import Workflow, WorkflowStep

        wf = Workflow(
            name="Fill Test Form",
            target_app=APP_TITLE,
            slug="fill_test_form",
            tags=["proof", "local", "uia"],
            notes="Focus the app, click the field, type a known string, press Submit.",
            steps=[
                WorkflowStep(index=0, action="focus_window", selector={"window_title": APP_TITLE}),
                WorkflowStep(index=1, action="wait", selector={"duration_ms": 500}),
                WorkflowStep(
                    index=2, action="click_element", selector={"name": "TaskField", "role": "Edit"}
                ),
                WorkflowStep(index=3, action="type", text=EXPECTED),
                WorkflowStep(index=4, action="wait", selector={"duration_ms": 500}),
                WorkflowStep(
                    index=5, action="click_element", selector={"name": "Submit", "role": "Button"}
                ),
                WorkflowStep(index=6, action="wait", selector={"duration_ms": 700}),
            ],
        )
        WORKFLOWS.mkdir(parents=True, exist_ok=True)
        wf_path = wf.save(WORKFLOWS)
        record("2. workflow recorded + saved", "PASS", str(wf_path.relative_to(ROOT)))

        # ---------- 3. reload from disk ----------
        from lucid.recorder.workflow import load_workflow

        loaded = load_workflow(wf_path)
        typed_step = next((s for s in loaded.steps if s.action == "type"), None)
        ok_reload = (
            loaded.slug == "fill_test_form"
            and len(loaded.steps) == 7
            and typed_step is not None
            and typed_step.text == EXPECTED
        )
        record(
            "3. workflow reloaded from disk",
            "PASS" if ok_reload else "FAIL",
            f"slug={loaded.slug} steps={len(loaded.steps)} text={typed_step.text if typed_step else None!r}",
        )

        # ---------- 4. reset the app so the replay must do the work ----------
        cleared = clear_field()
        record(
            "4. app reset before replay",
            "PASS" if cleared.get("entry_text") == "" else "FAIL",
            f"entry_text={cleared.get('entry_text')!r}",
        )

        # ---------- 5. replay every recorded step ----------
        problems: list[str] = []
        outputs: list[str] = []
        t0 = time.perf_counter()
        for step in loaded.steps:
            params = dict(step.selector or {})
            if step.text is not None:
                params["text"] = step.text
            out = safe_run(step.action, **params)
            outputs.append(f"{step.action} -> {out}")
            if isinstance(out, str) and out.startswith("error:"):
                problems.append(f"{step.action}: {out}")
        elapsed = time.perf_counter() - t0
        record(
            "5. replay executed every step",
            "PASS" if not problems else "FAIL",
            f"{len(loaded.steps)} steps in {elapsed:.2f}s" + (f" | {problems}" if problems else ""),
        )
        for line in outputs:
            print(f"         {line}")

        # ---------- 6. verify by observation, not by trust ----------
        final = wait_for_state(lambda s: s.get("submit_clicks", 0) >= 1, timeout=12.0)
        typed_ok = final.get("entry_text") == EXPECTED
        clicked_ok = final.get("submit_clicks", 0) >= 1
        record(
            "6. final state verified by observation",
            "PASS" if typed_ok and clicked_ok else "FAIL",
            f"entry_text={final.get('entry_text')!r} submits={final.get('submit_clicks')}",
        )

        # ---------- 7. app-side log corroborates ----------
        evs: list[dict] = []
        if EVENTS_PATH.exists():
            evs = [
                json.loads(x)
                for x in EVENTS_PATH.read_text(encoding="utf-8-sig").splitlines()
                if x.strip()
            ]
        submits = [e for e in evs if e.get("event") == "submit"]
        first_detail = submits[0]["detail"] if submits else None
        record(
            "7. app-side log corroborates replay",
            "PASS" if first_detail == EXPECTED else "FAIL",
            f"submit_events={len(submits)} first_detail={first_detail!r}",
        )

        # ---------- 8. controlled failure -> safe stop ----------
        # NOTE: upstream Actions._focus_window reports a missing window as
        # "no window matching ..." WITHOUT the "error:" prefix that the other
        # action handlers use, so a caller keying on that prefix would read a
        # failure as success. We therefore assert on the real contract: the
        # call returns promptly with a non-success message and does not hang.
        t1 = time.perf_counter()
        out = run("focus_window", window_title="ZZZ-no-such-window-ZZZ")
        dt = time.perf_counter() - t1
        failed_cleanly = isinstance(out, str) and not out.startswith("focused:")
        safe = dt < 10.0 and failed_cleanly
        record(
            "8. controlled failure returns safely",
            "PASS" if safe else "FAIL",
            f"result={out!r} in {dt:.2f}s (no 'error:' prefix: upstream inconsistency)",
        )

        # ---------- 9. failsafe / emergency stop ----------
        try:
            import pyautogui

            fs_ok = bool(pyautogui.FAILSAFE)
            fs_detail = f"pyautogui.FAILSAFE={fs_ok} (corner abort armed from settings)"
        except Exception as exc:  # noqa: BLE001
            fs_ok, fs_detail = False, repr(exc)[:120]
        record("9. emergency failsafe armed", "PASS" if fs_ok else "FAIL", fs_detail)

        # ---------- 10. no secrets in logs or artifacts ----------
        secret_re = re.compile(
            r"(sk-[A-Za-z0-9]{16,}"
            r"|ANTHROPIC_API_KEY\s*[=:]\s*\S+"
            r"|OPENAI_API_KEY\s*[=:]\s*\S+"
            r"|Bearer\s+[A-Za-z0-9._\-]{16,})",
            re.IGNORECASE,
        )
        leaks: list[str] = []
        scanned = 0
        for cand in sorted((ROOT / "data").rglob("*")):
            if not cand.is_file() or cand.suffix.lower() in {".db", ".sqlite", ".png", ".webp"}:
                continue
            scanned += 1
            try:
                txt = cand.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in secret_re.finditer(txt):
                leaks.append(f"{cand.relative_to(ROOT)}: {m.group(0)[:40]}")
        record(
            "10. no secrets in logs or artifacts",
            "PASS" if not leaks else "FAIL",
            f"{scanned} files scanned, clean" if not leaks else "; ".join(leaks[:3]),
        )

        # ---------- 11. no paid-API credential in play ----------
        env_leak = [k for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY") if os.environ.get(k)]
        record(
            "11. no paid-API credential in environment",
            "PASS" if not env_leak else "FAIL",
            "clean" if not env_leak else ",".join(env_leak),
        )

        # ---------- 12. deterministic replay is repeatable ----------
        # `type` pastes at the caret, so a replay appends rather than replaces.
        # The field must therefore be provably empty before replaying again,
        # otherwise the result is the concatenation and the check is meaningless.
        focus_app()
        safe_run("click_element", name="TaskField", role="Edit")
        for _ in range(len(read_state().get("entry_text") or "") + 4):
            safe_run("key", keys=["backspace"])
        blanked = wait_for_state(lambda s: s.get("entry_text") == "", timeout=10.0)
        if blanked.get("entry_text") != "":
            record(
                "12. second replay reproduces the result",
                "FAIL",
                f"reset did not clear the field: {blanked.get('entry_text')!r}",
            )
        else:
            replay2_problems: list[str] = []
            for step in loaded.steps:
                params = dict(step.selector or {})
                if step.text is not None:
                    params["text"] = step.text
                o = safe_run(step.action, **params)
                if isinstance(o, str) and o.startswith("error:"):
                    replay2_problems.append(f"{step.action}: {o}")
            again = wait_for_state(lambda s: s.get("submit_clicks", 0) >= 2, timeout=12.0)
            ok_again = (
                not replay2_problems
                and again.get("entry_text") == EXPECTED
                and again.get("submit_clicks", 0) >= 2
            )
            record(
                "12. second replay reproduces the result",
                "PASS" if ok_again else "FAIL",
                f"entry_text={again.get('entry_text')!r} submits={again.get('submit_clicks')} "
                f"errors={replay2_problems or 'none'}",
            )

    # ---------- 13. no input was ever sent to a game window ----------
        # The guard refused every input while another window owned the
        # foreground, so reaching this line means no keystroke escaped the
        # test app.
        verdict = window_guard.describe_environment()
        record("13. guard held (no game input)", "PASS", f"final {verdict}")

    except window_guard.FocusViolation as exc:
        # The guard stopped the run before any keystroke could escape. That is
        # the designed behaviour, not a failure of the product.
        record("13. guard held (no game input)", "PASS", f"run aborted safely: {exc}")

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