"""Probe: can the Windows accessibility tree see the test app's controls?

Launches scripts/test_app.ps1 (WinForms), then walks the UIA tree and prints
what it finds. This isolates "is the target app UIA-visible" from "does the
replay work", so a failure in the proof has a single obvious cause.

Run:  .venv\\Scripts\\python.exe scripts\\probe_uia.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
STATE_PATH = ROOT / "data" / "testapp_state.json"
EVENTS_PATH = ROOT / "data" / "testapp_events.log"


def main() -> int:
    import uiautomation as auto

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
    print(f"launched pid={app.pid}")
    time.sleep(6.0)

    try:
        win = auto.WindowControl(searchDepth=3, RegexName=r"ReplayPilot Test App")
        if not win.Exists(5.0):
            print("RESULT: FAIL -- test app window not found in UIA tree")
            return 2
        print(f"RESULT: window found -> {win.Name!r}")

        win.SetActive()
        time.sleep(1.0)

        found: list[str] = []
        stack: list[tuple[object, int]] = [(win, 0)]
        while stack:
            node, depth = stack.pop()
            if depth > 5:
                continue
            try:
                nm = (getattr(node, "Name", "") or "").strip()
                ct = (getattr(node, "ControlTypeName", "") or "").strip()
                if nm:
                    found.append(f"{'  ' * depth}{ct}: {nm!r}")
            except Exception:
                continue
            try:
                for ch in node.GetChildren() or []:
                    stack.append((ch, depth + 1))
            except Exception:
                continue

        print("UIA tree under the app window:")
        for line in found:
            print("  " + line)

        names = " | ".join(found).lower()
        has_field = "taskfield" in names
        has_submit = "submit" in names
        print()
        print(f"RESULT: TaskField visible={has_field}  Submit visible={has_submit}")
        return 0 if (has_field and has_submit) else 2
    finally:
        app.terminate()
        try:
            app.wait(timeout=5)
        except subprocess.TimeoutExpired:
            app.kill()


if __name__ == "__main__":
    raise SystemExit(main())