"""Direct check of _parse_key_list against the measured shapes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lucid.backend.lm_studio_backend import _parse_key_list  # noqa: E402

cases = [
    ("['ctrl','l']", ["ctrl", "l"]),      # Python literal (measured in dry run)
    ('["ctrl","l"]', ["ctrl", "l"]),      # JSON
    ("ctrl+a", ["ctrl", "a"]),            # joined
    ("delete", ["delete"]),               # single
    ("hello world", []),                  # prose
    ("", []),                             # empty
]

failures = 0
for raw, expected in cases:
    got = _parse_key_list(raw)
    status = "OK " if got == expected else "BAD"
    if got != expected:
        failures += 1
    print(f"[{status}] {raw!r:20} -> {got!r} (expected {expected!r})")

print(f"\n{len(cases) - failures}/{len(cases)} correct")
raise SystemExit(1 if failures else 0)