"""Replay the live-run request through Lucid's own provider path.

Case A/B (raw OpenAI SDK) return native tool_calls. The live run got
JSON-in-text. Remaining differences: stream=True, and Lucid's
_to_openai_message translation. This script isolates both.

Background-safe: HTTP to localhost, no desktop input.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lucid.agent.execute_mode import SYSTEM_PROMPT  # noqa: E402
from lucid.config.settings import get_settings  # noqa: E402
from lucid.llm.anthropic_client import build_computer_tool  # noqa: E402
from lucid.llm.provider import Message  # noqa: E402
from lucid.llm.registry import create_provider_by_name  # noqa: E402


def build_messages() -> list[Message]:
    return [
        Message(
            role="user",
            content=[
                {"type": "text", "text": "You are Lucid in autonomous mode."},
            ],
        ),
        Message(
            role="user",
            content=[
                {
                    "type": "text",
                    "text": (
                        "The window 'ReplayPilot Test App' is in the foreground. "
                        "Click the text field named TaskField, type exactly "
                        "EXECUTEOK-42 into it, then click the Submit button.\n\n"
                        "Accessibility tree:\n"
                        "  EditControl 'TaskField'  (rect 428,255 -> 552,281)\n"
                        "  ButtonControl 'Submit'   (rect 184,296 -> 324,332)\n"
                    ),
                }
            ],
        ),
    ]


def main() -> int:
    settings = get_settings()
    inner = create_provider_by_name("lm_studio", settings)
    inner._model = settings.execute_model
    inner._probed = True

    tool = build_computer_tool(2560, 1440)
    msgs = build_messages()

    print("--- Lucid provider path, stream=True, execute_model pin ---")
    print(f"provider={type(inner).__name__} model={settings.execute_model}")

    collected: list[str] = []
    native_calls: list[str] = []
    for ev in inner.stream(
        msgs, system=SYSTEM_PROMPT, tools=[tool], max_tokens=600, model=settings.execute_model
    ):
        if ev.kind == "text_delta":
            collected.append(ev.text)
        elif ev.kind == "tool_use":
            native_calls.append(f"{ev.tool_use.action} {json_min(ev.tool_use.raw)}")
        elif ev.kind == "error":
            print(f"  ERROR event: {str(ev.error)[:200]}")
        elif ev.kind == "done":
            print(f"  done stop_reason={ev.stop_reason}")

    if native_calls:
        print(f"  NATIVE TOOL_USE ({len(native_calls)}):")
        for c in native_calls:
            print(f"    {c[:150]}")
    else:
        print("  NO native tool_use events.")
    text = "".join(collected).strip()
    if text:
        print(f"  content ({len(text)} chars): {text[:300]!r}")
    return 0


def json_min(raw: dict) -> str:
    import json

    try:
        return json.dumps(raw.get("input", {}), separators=(",", ":"))
    except Exception:
        return "?"


if __name__ == "__main__":
    raise SystemExit(main())