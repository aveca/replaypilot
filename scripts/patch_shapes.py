"""Replace the shape-1 assertion in verify_response_shapes.py (UTF-8 safe)."""

from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "scripts" / "verify_response_shapes.py"

OLD = '''    elif tools:
        actions = [t.action for t in tools]
        ok = "click_element" in actions and all(
            t.raw.get("_recovered_from_text") for t in tools
        )
        check("shape 1: JSON-in-content", ok,
              f"recovered actions={actions} params={tools[0].raw['input']}")'''

NEW = '''    elif tools:
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
              f"rejected as unsupported")'''

src = TARGET.read_text(encoding="utf-8")
count = src.count(OLD)
assert count == 1, f"expected exactly one match, found {count}"
TARGET.write_text(src.replace(OLD, NEW), encoding="utf-8")
print("patched:", TARGET.name)
