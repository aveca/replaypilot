"""Verify the three measured response shapes against the real local model.

No GUI, no desktop input: HTTP to localhost only. Each shape below was
observed in real runs and must now be handled by the Lucid provider path.

  shape 1: the action written as JSON in the content (no native tool_calls)
  shape 2: a native tool_call whose arguments omit "action"
  shape 3: keys written as a Python literal string

Run:  .venv\\Scripts\\python.exe scripts\\verify_response_shapes.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lucid.config.settings import get_settings  # noqa: E402
from lucid.llm.anthropic_client import build_computer_tool  # noqa: E402
from lucid.llm.provider import Message  # noqa: E402
from lucid.llm.registry import create_provider_by_name  # noqa: E402

GOAL = (
    "The window 'ReplayPilot Test App' is in the foreground. Click the text "
    "field named TaskField, type exactly EXECUTEOK-42 into it, then click the "
    "Submit button.\n\nAccessibility tree:\n"
    "  EditControl 'TaskField'\n  ButtonControl 'Submit'\n"
)

A11Y_CONTEXT = (
    "You are Lucid in autonomous mode. You control the user's mouse and "
    "keyboard through the `computer` tool. PREFER keyboard and SEMANTIC "
    "actions OVER pixel clicks. Use click_element with element_name. "
    "Emit exactly one action at a time."
)


def shape1_prompt() -> str:
    """Ask for prose output to reproduce the JSON-in-content drift."""
    return (
        "Do NOT use the computer tool. Reply with a single JSON object only, "
        "of the form {\"name\": ..., \"parameters\": {...}}, describing the "
        "single next action: click the element named TaskField."
    )


def main() -> int:
    settings = get_settings()
    inner = create_provider_by_name("lm_studio", settings)
    inner._model = settings.execute_model
    inner._probed = True
    tool = build_computer_tool(2560, 1440)

    print(f"model={settings.execute_model} url={settings.backend.lm_studio_url}")
    print("-" * 70)

    results: list[tuple[str, str, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append((name, "PASS" if ok else "FAIL", detail))
        print(f"[{'PASS' if ok else 'FAIL':4}] {name} -- {detail[:150]}")

    def run_stream(msgs, **kw):
        text, tools = [], []
        for ev in inner.stream(msgs, **kw):
            if ev.kind == "text_delta":
                text.append(ev.text)
            elif ev.kind == "tool_use":
                tools.append(ev.tool_use)
            elif ev.kind == "error":
                return "".join(text), tools, ev.error
        return "".join(text), tools, None

    # ---- shape 1: JSON in content, recovered as an action ----
    msgs = [Message(role="user", content=[inner.text_block(shape1_prompt())])]
    text, tools, err = run_stream(msgs, max_tokens=200)
    if err:
        check("shape 1: JSON-in-content", False, f"error: {err[:120]}")
    elif tools:
        # The recovery mechanism is proven if the model's prose JSON became a
        # tool_use marked as recovered. The action NAME is the model's choice
        # and may be hallucinated (measured: "clickTaskField"); the safety
        # property is that an unknown name is rejected by the dispatch table,
        # never executed. Both halves are asserted below.
        recovered = all(t.raw.get("_recovered_from_text") for t in tools)
        check("shape 1: prose JSON recovered as a tool_use",
              bool(tools) and recovered,
              f"recovered={[t.action for t in tools]} "
              f"params={tools[0].raw['input']}")
        from lucid.executor.actions import Actions as _A
        from lucid.llm.schemas import ActionBlock as _AB

        dispatcher = _A(settings)
        rejected = 0
        for t in tools:
            out = dispatcher.run(
                _AB(id=f"v-{t.action}", action=t.action, params=t.raw["input"])
            )
            if out.startswith("unsupported action:"):
                rejected += 1
        check("shape 1: hallucinated action names are rejected, not executed",
              rejected == len(tools) or not tools,
              f"names={[t.action for t in tools]} -> {rejected}/{len(tools)} "
              f"rejected as unsupported")
    else:
        check("shape 1: JSON-in-content", False,
              f"no tool_use recovered; content={text.strip()[:120]!r}")

    # ---- shapes 2 and 3: native tool path with the Execute-style prompt ----
    msgs = [
        Message(role="user", content=[inner.text_block(A11Y_CONTEXT)]),
        Message(role="user", content=[inner.text_block(GOAL)]),
    ]
    text, tools, err = run_stream(
        msgs, system=A11Y_CONTEXT, tools=[tool], max_tokens=400,
        model=settings.execute_model,
    )
    if err:
        check("shapes 2+3: native tool path", False, f"error: {err[:120]}")
    else:
        named = [t for t in tools if t.action]
        unnamed = [t for t in tools if not t.action]
        check("shapes 2+3: every tool_call has a non-empty action",
              bool(tools) and not unnamed,
              f"actions={[t.action for t in tools]}"
              + (f" UNNAMED={len(unnamed)}" if unnamed else ""))
        check("shapes 2+3: semantic sequence present",
              "click_element" in [t.action for t in named]
              and "type" in [t.action for t in named],
              f"first three={[t.action for t in named[:3]]}")
        typed = [t for t in tools if t.text == "EXECUTEOK-42"]
        check("shapes 2+3: payload carried exactly", bool(typed),
              f"text={typed[0].text!r}" if typed else "no type action with the payload")

    print("-" * 70)
    passed = sum(1 for _, s, _ in results if s == "PASS")
    print(f"{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())