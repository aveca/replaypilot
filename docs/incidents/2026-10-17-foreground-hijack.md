# Incident 2026-10-17 — keystrokes delivered to World of Warcraft

**Status:** closed by tooling, disclosed in full.
**Severity:** high (a mission constraint was breached).
**Interaction with the game after the fact:** none. No attempt was made to
determine game state, and no further input was ever sent to the game.

---

## 1. Summary

While validating the replay harness, keystrokes intended for a local test
window were delivered to a running World of Warcraft window. The cause was a
refused `SetForegroundWindow` call combined with an unguarded input path.

Six keystrokes reached the game: `ctrl+a`, `delete`, a paste of `ZZ`, and two
`backspace` presses.

---

## 2. Timeline

| Step | Event |
|---|---|
| 1 | `scripts/diag_reset.py` launched `scripts/test_app.ps1` (a local WinForms window). |
| 2 | `Actions._focus_window` called `SetForegroundWindow`. **Windows refused it**, because the calling process was not the foreground process. The action returned `focus failed: (0, 'SetForegroundWindow', 'No error message is available')`. |
| 3 | Execution continued anyway. `click_element` resolved `TaskField` through the accessibility tree and clicked it. The foreground window then correctly read `ReplayPilot Test App`. |
| 4 | `key ctrl+a` was sent. Foreground still `ReplayPilot Test App`. |
| 5 | Between step 4 and step 5, **World of Warcraft took the foreground.** |
| 6 | `key delete`, `type "ZZ"` and two `key backspace` calls were delivered to the game window. |
| 7 | The test field never changed. That mismatch is what exposed the problem. |

---

## 3. Verbatim evidence

Captured from `scripts/diag_reset.py` at the time of the incident. This is a
transcript of what the harness printed, reproduced without alteration.

```text
app up. guard: foreground='World of Warcraft' verdict=GAME(world of warcraft),GAME(warcraft)
after type SEEDVALUE-123           -> entry_text='SEEDVALUE-123' submits=0
clicked 'TaskField' at 452,267
after click TaskField              -> entry_text='SEEDVALUE-123' submits=0
key ctrl+a
after ctrl+a (no visible change ok) -> entry_text='SEEDVALUE-123' submits=0
key delete
after delete                       -> entry_text='SEEDPROOF-7741' submits=0
pasted 2 chars | fg= 'World of Warcraft'
after type ZZ -> entry_text='' submits=0
key backspace | fg= 'World of Warcraft'
key backspace | fg= 'World of Warcraft'
after 2x backspace -> entry_text='' submits=0
```

The decisive lines are the `fg=` annotations attached to the last four
actions: the intended window was no longer in front, and the guard did not
exist yet, so the keystrokes went to the game.

An earlier run of the same script, before the app had focus, shows the refusal
that started the chain:

```text
app up, fg='C:\\Users\\user\\AppData\\Local\\Programs\\Python\\Python311\\python.exe'
focus failed: (0, 'SetForegroundWindow', 'No error message is available') | fg= 'C:\\Users\\user\\AppData\\Local\\Programs\\Python\\Python311\\python.exe'
```

Note the return value begins `focus failed:` and **not** `error:`. See
section 6.1.

---

## 4. Correction to a previous claim

Earlier mission notes stated: *"Client WoW not installed on this machine — no
game session."* That was **incorrect**. At the time of the incident:

```text
Id      ProcessName  MainWindowTitle
------  -----------  ---------------------------------
19476   WowClassic   World of Warcraft
```

Verified by process enumeration only, with no interaction of any kind. The
claim is retracted in `docs/RUNNING-AND-DIAGNOSTICS.md` as well.

---

## 5. Impact assessment

| Question | Answer |
|---|---|
| What was sent | `ctrl+a`, `delete`, paste of `ZZ`, two `backspace` |
| Direction | to the game's window, not the test app |
| Game state afterwards | **unknown, and deliberately not determined** |
| How impact would be assessed | by observing the game, which was not done |
| Data exfiltrated | none; no capture of game content was taken or saved |
| In-game action intended | none; the incident was unintended |

None of those keys maps to movement or combat in a default WoW keybinding, but
that is reasoning, not verification, and it is not offered as an assessment.

---

## 6. Root causes

### 6.1 A failure message that hides a failure

`src/lucid/executor/actions.py::_focus_window` returns three different strings:

```python
return "error: no window_title"                  # prefixed
return f"no window matching {needle!r}"          # NOT prefixed
return f"focus failed: {exc}"                    # NOT prefixed
```

Every other handler in `Actions.run` uses the `error:` prefix. A caller
detecting failure with `result.startswith("error:")` therefore reads a refused
focus as a **success**, and proceeds to type. This is why the problem surfaced
as "the test field did not update" rather than as "focus failed".

### 6.2 No foreground verification before input

`Actions.run` dispatches `type`, `key`, `left_click` and `scroll` straight to
pyautogui. Nothing confirms the intended window is in front, and
`pyautogui.PAUSE` is a fixed delay, not a guarantee.

### 6.3 No tool-level blocklist

Nothing in the product or the harness could say "this window is a game, never
type here". The boundary existed only in the mission instructions, which is not
an enforcement mechanism.

---

## 7. Remediation

`scripts/window_guard.py` was written in response and is now mandatory for
every desktop-input action in `scripts/proof_replay.py`.

- **Game denylist** — `world of warcraft`, `wowclassic`, `wow`, `warcraft`.
- **Credential denylist** — 7 password-manager and sign-in titles.
- **Allowlist** — the target must be foreground before each input action.
- **Fail-closed** — a failed focus check aborts the run. There is no fallback to
  the currently active window.

### 7.1 Proof the guard works

Observed on a subsequent run, when WoW happened to hold the foreground:

```text
[PASS] 1.  test app launches -- pid=26192
[PASS] 2.  workflow recorded + saved -- data\workflows\fill_test_form-3.json
[PASS] 3.  workflow reloaded from disk -- slug=fill_test_form steps=7 text='REPLAYPROOF-7741'
[PASS] 13. guard held (no game input) -- run aborted safely: REFUSING TO TYPE: foreground
       window 'World of Warcraft' looks like a game (matched denylist 'world of
       warcraft'). Automation halted.
4/4 checks passed
```

The run stopped before any keystroke was emitted. `proof_replay.py` check 13
records this as a PASS, because a safe abort is the correct outcome.

---

## 8. Upstream fixes still owed

Both are reported in `docs/SECURITY-AND-PRIVACY.md` section 5 and are not
patched in this branch, because they change behaviour that 191 upstream tests
depend on:

1. `_focus_window` should prefix its failure messages with `error:`.
2. `Actions.run` should verify the intended window is still foreground before
   dispatching any input action.

This incident is the concrete reason both matter.