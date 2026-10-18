"""Reproduce the exact Execute request against the execute model.

Background-safe: HTTP to localhost only, no desktop input, no focus.

The live run showed the model answering with JSON *in the content* instead of
native tool_calls, so the executor saw "no action emitted" and nudged four
times. This script isolates which part of the request shape causes it, by
replaying the same request with and without the Execute system prompt.

Run:  .venv\\Scripts\\python.exe scripts\\repro_execute_request.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from openai import OpenAI  # noqa: E402

from lucid.agent.execute_mode import SYSTEM_PROMPT  # noqa: E402
from lucid.config.settings import get_settings  # noqa: E402
from lucid.llm.anthropic_client import build_computer_tool  # noqa: E402
from lucid.backend.lm_studio_backend import _to_openai_tool  # noqa: E402


def run_case(label: str, *, with_system: bool, with_tools: bool) -> None:
    settings = get_settings()
    client = OpenAI(
        base_url=settings.backend.lm_studio_url,
        api_key=settings.backend.lm_studio_api_key,
        timeout=90.0,
    )
    model = settings.execute_model

    messages = []
    if with_system:
        messages.append({"role": "system", "content": SYSTEM_PROMPT})
    messages.append(
        {
            "role": "user",
            "content": (
                "The window 'ReplayPilot Test App' is in the foreground. Click "
                "the text field named TaskField, type exactly EXECUTEOK-42 into "
                "it, then click the Submit button. Do nothing else.\n\n"
                "Accessibility tree:\n"
                "  EditControl 'TaskField'  (rect 428,255 -> 552,281)\n"
                "  ButtonControl 'Submit'   (rect 184,296 -> 324,332)\n"
            ),
        }
    )

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": 300,
        "stream": False,
    }
    if with_tools:
        kwargs["tools"] = [_to_openai_tool(build_computer_tool(2560, 1440))]
        kwargs["tool_choice"] = "auto"

    print(f"--- {label}: system={with_system} tools={with_tools} ---")
    try:
        resp = client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        tc = msg.tool_calls
        if tc:
            for call in tc:
                print(f"  NATIVE TOOL_CALL: {call.function.name}({call.function.arguments})")
        else:
            print(f"  NO native tool_calls. content={msg.content!r}"[:400])
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR: {type(exc).__name__}: {str(exc)[:200]}")
    print()


def main() -> int:
    # One case at a time so a single slow request cannot eat the whole budget.
    cases = [
        ("A: probe shape (no system, with tools)", False, True),
        ("B: Execute shape (system + tools)", True, True),
    ]
    if "--all" in sys.argv:
        cases.append(("C: Execute prompt but NO tools param", True, False))

    which = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    if which is not None:
        label, ws, wt = cases[int(which)]
        cases = [(label, ws, wt)]

    for label, ws, wt in cases:
        run_case(label, with_system=ws, with_tools=wt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())