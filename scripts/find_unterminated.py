"""Locate the unterminated string in window_guard.py."""

import io
import tokenize
from pathlib import Path

src = Path(__file__).resolve().parents[1] / "scripts" / "window_guard.py"
raw = src.read_bytes()

# Count triple-double-quotes per line to find the imbalance.
lines = raw.decode("utf-8").splitlines()
tq = '"""'
state = "closed"
for i, line in enumerate(lines, 1):
    n = line.count(tq)
    if n % 2 == 1:
        state = "open" if state == "closed" else "closed"
        print(f"line {i:4}: toggle -> {state}   | {line.strip()[:90]}")

print(f"\nfinal state: {state}")
print(f"total triple quotes: {raw.decode('utf-8').count(tq)}")

try:
    import ast

    ast.parse(raw.decode("utf-8"))
    print("parses: OK")
except SyntaxError as exc:
    print(f"parses: SyntaxError at line {exc.lineno}: {exc.msg}")
