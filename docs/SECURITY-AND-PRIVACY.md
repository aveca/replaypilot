# Security and privacy

ReplayPilot drives the real mouse and keyboard of the machine it runs on.
This document states what was actually verified, what is enforced in code, and
one incident that occurred during validation and changed how the harnesses work.

---

## 1. Data leaves the machine

| Path | Destination | Verified |
|---|---|---|
| LLM requests | `http://localhost:11434/v1` (loopback) | PASS |
| Telemetry | disabled (`telemetry.enabled: false`) | PASS, config |
| Updater | disabled (`updater.enabled: false`) | PASS, config |
| MCP servers | disabled (`mcp.enabled: false`) | PASS, config |
| Browser actions | disabled (`browser.enabled: false`) | PASS, config |

**Evidence for loopback-only.** A `Get-NetTCPConnection` snapshot was taken
before and after a full `scripts/probe_ollama.py` run. The set of established
remote addresses was **unchanged**: no new endpoint appeared. The LLM exchange
therefore went over loopback. Credentials are absent by design —
`lm_studio_api_key: ollama-local` is a placeholder Ollama ignores, and no
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` is present in the environment.

**Evidence for secret hygiene.** `proof_replay.py` check 10 scans every text
artifact under `data/` for API-key shapes (`sk-…`, `ANTHROPIC_API_KEY=…`,
`Bearer …`). Result: clean across 4–5 files.

---

## 2. What ran during validation

Everything was aimed at `scripts/test_app.ps1`, a local WinForms window that
does nothing but mirror typed text into a JSON file. No game, no browser, no
production application was targeted.

However — see section 3.

---

## 3. Incident: keystrokes reached an unintended window

**This is the most important thing in this document.**

During an early run of `scripts/diag_reset.py`, the following happened:

1. `Actions._focus_window` called `SetForegroundWindow`, which **Windows
   refused** because the calling process was not the foreground process. The
   action returned `focus failed: (0, 'SetForegroundWindow', ...)`.
2. Execution continued. `click_element` then succeeded and the test field did
   receive its clicks.
3. Between two actions, **World of Warcraft (`WowClassic.exe`) took the
   foreground**. `scripts/diag_reset.py` printed
   `fg= 'World of Warcraft'`.
4. The subsequent keystrokes — `ctrl+a`, `delete`, a paste of `ZZ`, and two
   `backspace` presses — were delivered by pyautogui to **that window**, not to
   the test app. The test app's field stayed empty, which is how the problem
   was noticed.

Earlier mission notes recorded "WoW client not installed on this machine".
That was **wrong**. `WowClassic.exe` was running, window title
`World of Warcraft`, PID 19476.

**Impact.** A small number of neutral keys were delivered to a game window.
Whether that altered game state could not be determined without interacting
with the game, which was not done. This was an unintended breach of the
mission rule *do not send in-game actions* and is disclosed rather than
minimised.

**Root cause.** No component checked which window was in the foreground before
sending input. Lucid assumes `focus_window` succeeding; when Windows refuses
it, the failure is reported but subsequent actions fire regardless.

---

## 4. Controls added in response

### `scripts/window_guard.py`

Every desktop-input action is now preceded by a check, and the run aborts if the
check fails.

- **Game denylist.** `world of warcraft`, `wowclassic`, `wow`, `warcraft`. If
  any of these is the foreground window, the harness raises `FocusViolation`
  and sends nothing.
- **Credential denylist.** `bitwarden`, `1password`, `keepass`, `credential`,
  `password`, `sign-in`, `login`. Nothing is typed into these.
- **Allowlist.** `check(expected_title)` asserts the expected window is still in
  front. `proof_replay.py` calls this before every `type`, `key`, `click_element`
  and `scroll` step.
- **Reliable foregrounding.** `force_foreground()` uses the documented
  `AttachThreadInput` workaround so raising *our own* test window succeeds where
  `SetForegroundWindow` was refused. It only raises windows; it never types.

Observed behaviour after the change:

```
guard: foreground='OpenCode' verdict=clear
...
[PASS] 13. guard held (no game input) -- final foreground='ReplayPilot Test App' verdict=clear
```

### Emergency stops that exist in the product

| Control | Source | State |
|---|---|---|
| `ctrl+shift+k` kill switch | `safety.kill_switch_hotkey` | configured |
| Corner abort | `safety.failsafe` -> `pyautogui.FAILSAFE` | **PASS**, armed at runtime |
| Destructive-action confirmation | `safety.destructive_confirm: true` | configured |
| Screenshot blacklist | `ScreenshotSettings.blacklist_titles` | 5 password-manager titles |

---

## 5. Upstream defects found

Neither is patched in this branch. Both are reported so the fix can be made
deliberately rather than as a side effect of validation.

### 5.1 Failure messages are inconsistent (moderate)

`src/lucid/executor/actions.py::_focus_window` returns:

- `"error: no window_title"` — prefixed
- `"no window matching {needle!r}"` — **not** prefixed
- `"focus failed: {exc}"` — **not** prefixed

Every other handler in the same class uses the `error:` prefix. A caller that
detects failure with `result.startswith("error:")` will read a failed focus as
a success — which is precisely how the incident in section 3 stayed invisible
until the test app failed to update.

*Suggested fix:* return `f"error: no window matching {needle!r}"`.

### 5.2 No foreground verification before input (moderate)

`Actions.run` dispatches `type`, `key`, `left_click`, `scroll` and friends
straight to pyautogui. Nothing confirms the intended window is in front, and
`pyautogui`'s own `PAUSE` is a fixed delay, not a guarantee.

*Suggested fix:* resolve the target window before any input action and abort if
it is no longer foreground, ideally reusing the denylist approach in
`scripts/window_guard.py`.

---

## 6. Screen capture and privacy

- `capture.mode: a11y_only` is the shipped default, so **no screenshot is sent
  to the model** in the validated configuration.
- With a vision model, screenshots would be base64-encoded in the request body.
  That body stays on loopback, but it does mean screen contents pass through the
  model.
- `screenshot.persist: true` by default, written under `data/screenshots`.
  `screenshot.retention_hours` bounds the footprint.
- `screenshot.blacklist_titles` suppresses capture for password-manager and
  private-browsing windows.

---

## 7. Scope boundary: World of Warcraft

ReplayPilot is **not** to be used to act inside World of Warcraft.

- No anti-cheat evasion, no input masking, no memory inspection of the client.
- Use it only for out-of-game diagnostics: window state, screenshots of
  non-game windows, logs, procedures in ordinary applications.
- The game denylist in `window_guard.py` exists to make that boundary
  enforceable by tooling rather than by intention alone.
- Blizzard's terms and any applicable anti-cheat rules remain the user's
  responsibility; nothing here constitutes advice about them.

---

## 8. Reporting

Found a hole? Open an issue on the fork. Do not include screenshots containing
personal data, tokens or session material in the report.