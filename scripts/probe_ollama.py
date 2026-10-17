"""Real compatibility probe: Lucid's LMStudioProvider -> Ollama.

Run:  .venv\Scripts\python.exe scripts\probe_ollama.py

No network egress beyond 127.0.0.1. No API key. Reports PASS/FAIL per check.
"""

from __future__ import annotations

import json
import sys
import time

from lucid.config.settings import get_settings
from lucid.llm.provider import Message
from lucid.llm.registry import available_providers, create_provider_by_name

RESULTS: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str) -> None:
    RESULTS.append((name, status, detail))
    print(f"[{status:4}] {name} -- {detail}")


def main() -> int:
    settings = get_settings()

    # Allow probing a specific model without editing settings.yaml.
    override = None
    if "--model" in sys.argv:
        override = sys.argv[sys.argv.index("--model") + 1]
        settings.backend.lm_studio_model = override
    if "--url" in sys.argv:
        settings.backend.lm_studio_url = sys.argv[sys.argv.index("--url") + 1]

    print(f"provider={settings.provider} mode={settings.backend.mode}")
    print(f"url={settings.backend.lm_studio_url}")
    print(f"model={settings.backend.lm_studio_model}")
    print("-" * 68)

    record("providers registered", "PASS", ", ".join(available_providers()))

    try:
        provider = create_provider_by_name("lm_studio", settings)
    except Exception as exc:  # noqa: BLE001
        record("provider construction", "FAIL", repr(exc))
        return 1
    record("provider construction", "PASS", type(provider).__name__)

    # ---- Check 1: plain text completion ----
    msgs = [Message(role="user", content=[provider.text_block("Reply with exactly: LOCAL_OK")])]
    t0 = time.perf_counter()
    text, err = [], None
    for ev in provider.stream(msgs, system="You are a connectivity probe.", max_tokens=32):
        if ev.kind == "text_delta":
            text.append(ev.text)
        elif ev.kind == "error":
            err = ev.error
    dt = time.perf_counter() - t0
    out = "".join(text).strip()
    if err:
        record("text completion", "FAIL", f"{err} ({dt:.1f}s)")
    else:
        record("text completion", "PASS", f"{out!r} in {dt:.1f}s")

    # ---- Check 2: vision capability ----
    # A 200 with prose is NOT proof of vision. A text-only model answers with a
    # refusal or a generic request for more context, and the round trip still
    # succeeds. We therefore report the raw answer and classify honestly.
    vision_text = ""
    vision_err = None
    try:
        from PIL import Image

        # A flat blue square: trivial to describe IF the model can see.
        img = Image.new("RGB", (96, 96), (10, 90, 200))
        block = provider.image_block(img)
        vmsgs = [
            Message(
                role="user",
                content=[
                    block,
                    provider.text_block(
                        "This image is a single flat colour. Reply with only the "
                        "dominant colour name, or the single word BLIND if you "
                        "cannot see images."
                    ),
                ],
            )
        ]
        for ev in provider.stream(vmsgs, max_tokens=48):
            if ev.kind == "text_delta":
                vision_text += ev.text
            elif ev.kind == "error":
                vision_err = ev.error
    except Exception as exc:  # noqa: BLE001
        vision_err = repr(exc)

    answer = vision_text.strip()
    if vision_err:
        record("vision capability", "FAIL", f"request error: {str(vision_err)[:120]}")
    elif answer.upper().startswith("BLIND") or "cannot see" in answer.lower():
        record("vision capability", "FAIL", f"model is text-only: {answer[:100]!r}")
    elif "blue" in answer.lower():
        record("vision capability", "PASS", f"model described the image: {answer[:100]!r}")
    else:
        record("vision capability", "NOT_PROVEN", f"ambiguous answer: {answer[:100]!r}")

    # ---- Check 3: OpenAI function-calling (drives Execute mode) ----
    computer_tool = {
        "name": "computer",
        "description": "Control the computer: click, type, screenshot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["screenshot", "click", "type", "key", "scroll"],
                },
                "coordinate": {"type": "array", "items": {"type": "integer"}},
                "text": {"type": "string"},
                "keys": {"type": "string"},
            },
            "required": ["action"],
        },
    }
    cmsgs = [
        Message(
            role="user",
            content=[provider.text_block("Use the computer tool to take a screenshot. Action only, no prose.")],
        )
    ]
    calls, cerr = [], None
    try:
        for ev in provider.stream(cmsgs, tools=[computer_tool], max_tokens=256):
            if ev.kind == "tool_use":
                calls.append(ev.tool_use)
            elif ev.kind == "error":
                cerr = ev.error
    except Exception as exc:  # noqa: BLE001
        cerr = repr(exc)
    if cerr:
        record("tool calling", "FAIL", str(cerr)[:160])
    elif calls:
        c = calls[0]
        record("tool calling", "PASS", f"{c.action!r} args={json.dumps(c.raw)[:100]}")
    else:
        record("tool calling", "FAIL", "no tool_use event emitted (model answered with prose)")

    print("-" * 68)
    passed = sum(1 for _, s, _ in RESULTS if s == "PASS")
    print(f"{passed}/{len(RESULTS)} checks passed")
    print()
    print("Installed local models (from Ollama) and what each one is good for:")
    try:
        import httpx

        resp = httpx.get(f"{settings.backend.lm_studio_url.rstrip('/')}/models", timeout=5.0)
        for item in resp.json().get("data", []):
            mid = item.get("id", "?")
            low = mid.lower()
            vision = any(t in low for t in ("vl", "vision", "llava", "bakllava", "minicpm", "-v-"))
            print(f"  - {mid:34} vision={'yes' if vision else 'no '} tools=untested")
        print()
        print("Models whose name carries a vl/vision marker accept images. The")
        print("coder/chat models above are text-only. Whether a model emits OpenAI")
        print("tool_calls is only settled by check 3, which is model specific.")
    except Exception as exc:  # noqa: BLE001
        print(f"  (model listing unavailable: {exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())