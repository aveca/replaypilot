"""Apply a precise, UTF-8-safe replacement to test_adversarial.py."""

from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "tests" / "unit" / "test_adversarial.py"

OLD = '''def test_off_window_coordinates_never_emit(readers):
    readers2 = _readers_for(APP_HWND, APP_PID, APP_TITLE, "powershell")
    wg.install_readers(**readers2)
    session = wg.GuardedSession(_win())'''

NEW = '''def test_off_window_coordinates_never_emit(readers):
    readers2 = _readers_for(APP_HWND, APP_PID, APP_TITLE, "powershell")
    wg.install_readers(**readers2)
    # Input enabled on purpose: the callable below is a spy, never pyautogui,
    # so the only way it runs is if every check passes. This proves the
    # coordinate check gates emission rather than being bypassed by the switch.
    wg.set_input_enabled(True)
    session = wg.GuardedSession(_win())'''

src = TARGET.read_text(encoding="utf-8")
count = src.count(OLD)
assert count == 1, f"expected exactly one match, found {count}"
TARGET.write_text(src.replace(OLD, NEW), encoding="utf-8")
print("patched:", TARGET.name)
