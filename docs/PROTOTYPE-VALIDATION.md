# Prototype validation — ReplayPilot (local Ollama)

Validation of a functional Windows prototype built on a fork of Lucid, driven by
a local Ollama model. Every result below carries one of **PASS**, **FAIL**,
**NOT_PROVEN** or **BLOCKED**. Nothing here is asserted from documentation: each
line names the command that produced it.

- Date: 2026-10-17 (validation session 2 same day, see section 13)
- Machine: Windows 11, 2560x1440, interactive session 1, 31.6 GB RAM
- Python 3.11.9, uv 0.12.22, Ollama with OpenAI-compatible endpoint on `:11434/v1`

---

## 1. Repository and branch

| Item | Value |
|---|---|
| Fork | `https://github.com/aveca/replaypilot` |
| Origin remote | `https://github.com/aveca/replaypilot.git` |
| Upstream remote | `https://github.com/ertugrulakben/lucid.git` |
| Working branch | `feat/ollama-local-backend` |
| Base commit | `622110a` (identical to `upstream/main`) |
| Head commit | `8cbfff34814a5fd8fb9cd45ddabb7192a887a5dc` |
| Local path | `C:\replaypilot` |
| Upstream commits pushed | 0 — history and MIT notice preserved |

The fork carries Lucid's MIT licence and copyright notice unchanged
(`LICENSE`, `pyproject.toml`). Lucid's own repository was not modified.

**PASS** — fork exists under the `aveca` account, base commit identical to upstream.

---

## 2. How Ollama is actually reached

Two earlier claims were wrong. Both are corrected here because they change what
the code does.

| Earlier claim | Reality |
|---|---|
| "Lucid has a built-in Ollama backend" | **False.** `backend.mode` accepts `api`, `cli`, `lm_studio`. There is no `ollama` mode. |
| "WoW client is not installed on this machine" | **False.** `WowClassic.exe` was running, PID 19476. See section 7. |

Ollama is reached through `lm_studio`. The docstring of
`src/lucid/backend/lm_studio_backend.py` states the provider speaks the
OpenAI-compatible protocol and that "the same endpoint shape as Ollama's
OpenAI adapter, so this backend works for both with a URL swap."

```yaml
provider: lm_studio
model: qwen2.5-coder:7b
hotkey: "ctrl+alt+j"

backend:
  mode: lm_studio
  lm_studio_url: http://localhost:11434/v1
  lm_studio_model: qwen2.5-coder:7b
  lm_studio_api_key: ollama-local   # placeholder; Ollama ignores it

capture:
  mode: a11y_only
```

**PASS** — configuration loads; `lucid doctor` reports all five checks green.

```
[     OK]  API key configured  -- backend=lm_studio at http://localhost:11434/v1
[     OK]  Hotkey not in use by another app  -- `ctrl+alt+j` is free
[     OK]  Display scaling detected  -- DPI awareness level 2
[     OK]  Required permissions granted  -- writable: C:\replaypilot\data
[     OK]  Backend can reach the configured model
```

---

## 3. Defect found and fixed: images were silently dropped

This is the most consequential finding of the validation.

`LMStudioProvider.image_block()` returns the **OpenAI** shape:

```python
{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
```

`_to_openai_message()` only matched the **Anthropic** shape
`{"type": "image", "source": {"media_type": ..., "data": ...}}`. The image
block matched neither branch, so it was discarded with no warning and the
request carried text only.

Measured, same model, same prompt, two request builders:

| Request built by | Model | Answer |
|---|---|---|
| Lucid `LMStudioProvider` | `qwen2.5vl:3b` | `BLIND` |
| `openai` SDK, hand-built | `qwen2.5vl:3b` | `Blue` |

The model has vision; the provider was throwing the image away. Fix adds an
`image_url` branch to `_to_openai_message()`.

After the fix, through the real Lucid code path:

```
[PASS] vision capability -- model described the image: 'Blue'
```

**PASS** — bug fixed, regression covered by 5 new tests in
`tests/unit/test_local_backend.py`, including the exact failing case.

This bug is upstream, not local: any user of the `lm_studio` backend with a
vision model was affected and would have concluded their model was blind.

---

## 4. Model compatibility (measured, not assumed)

| Model | Size | Vision | Tools | Verdict |
|---|---|---|---|---|
| `qwen2.5-coder:7b` | 4.7 GB | FAIL — text-only | FAIL — answers in prose, no `tool_use` | text work only |
| `qwen2.5-coder:32k` | 4.7 GB | untested | untested | same family |
| `qwen2.5-coder-7b-16k` | 4.7 GB | untested | untested | same model, longer ctx |
| `llama3:8b` | 4.7 GB | FAIL | FAIL — HTTP 400 | unusable for Execute |
| `qwen2.5vl:3b` | 3.2 GB | **PASS** — answered `Blue` | FAIL — Ollama: `does not support tools` | vision, no tools |
| `qwen3:4b` | 2.5 GB | FAIL — `model does not support` | FAIL — prose, no `tool_use` | unusable |
| `llama3.1:8b` | 4.7 GB | FAIL — text-only | **PASS** — emitted `action: "screenshot"` | tools, no vision |
| `qwen3-coder:30b` | 18 GB | untested | untested | **NOT practical** — 18 GB vs 13.7 GB free |
| `qwen3.6:latest` | 23 GB | untested | untested | **NOT practical** — same |

- **PASS** — text completion against a local model, no external call.
- **PASS** — vision, via `qwen2.5vl:3b`, after the section 3 fix.
- **PASS** — OpenAI tool calling, via `llama3.1:8b`. It returned a real
  `tool_use` event: `action: "screenshot"`, `args={"input": {"action": "screenshot"}}`.
- **NOT practical** — the 30B and 23B models do not fit this machine's free
  memory, so the mission's caution against forcing a 30B model is confirmed by
  measurement rather than assumption.

### Execute mode is BLOCKED, for one precise reason

Two separate capabilities were each found in *different* models: vision in
`qwen2.5vl:3b`, tool calling in `llama3.1:8b`. Execute mode needs **both in the
same model**, because `agent/execute_mode.py` attaches a screenshot
unconditionally:

```python
# src/lucid/agent/execute_mode.py:281
content: list = [self.provider.image_block(snapshot.image)]
```

`capture.mode: a11y_only` does not rescue it. `ContextSnapshot.capture()` does
honour the setting and returns a blank image instead of a capture — measured
`image size=(16, 16) is_blank=True` — but `execute_mode` still wraps that blank
image in an `image_url` block. A text-only model then rejects the request:

```
ERROR: Error code: 400 - 'Multimodal data provided, but model does not
support multimodal requests.'
```

Reproduced against `llama3.1:8b`, the very model that *does* emit tool calls
correctly. So the blocker is not "no model has tools" and not "no model sees
images"; it is that a single small local model cannot do both.

- **BLOCKED** — Execute mode end to end on a local model. Needs one model with
  vision **and** tool calling that fits in this machine's memory, or a change
  to `execute_mode` so it omits the image block when `capture.mode` forbids a
  screenshot. That change is a product decision, not a validation fix, so it is
  not made here; it is the concrete next step in section 12.

Answer and Teach modes are **not** blocked: text works today, vision works
today.

---

## 5. Functional proof: record → save → reload → replay → verify

`scripts/proof_replay.py`, run twice, identical result both times.
Target: `scripts/test_app.ps1`, a local WinForms window that does nothing but
mirror typed text into a JSON file.

```text
[PASS] 1.  test app launches -- pid=28560
[PASS] 2.  workflow recorded + saved -- data\workflows\fill_test_form-1.json
[PASS] 3.  workflow reloaded from disk -- slug=fill_test_form steps=7 text='REPLAYPROOF-7741'
[PASS] 4.  app reset before replay -- entry_text=''
[PASS] 5.  replay executed every step -- 7 steps in 3.59s
         focus_window  -> focused: ReplayPilot Test App
         wait          -> waited 500ms
         click_element -> clicked 'TaskField' at 452,267
         type          -> pasted 16 chars
         wait          -> waited 500ms
         click_element -> clicked 'Submit' at 262,315
         wait          -> waited 700ms
[PASS] 6.  final state verified by observation -- entry_text='REPLAYPROOF-7741' submits=1
[PASS] 7.  app-side log corroborates replay -- submit_events=1 first_detail='REPLAYPROOF-7741'
[PASS] 8.  controlled failure returns safely -- result="no window matching 'zzz-no-such-window-zzz'" in 0.00s
[PASS] 9.  emergency failsafe armed -- pyautogui.FAILSAFE=True (corner abort armed from settings)
[PASS] 10. no secrets in logs or artifacts -- 5 files scanned, clean
[PASS] 11. no paid-API credential in environment -- clean
[PASS] 12. second replay reproduces the result -- entry_text='REPLAYPROOF-7741' submits=2 errors=none
[PASS] 13. guard held (no game input) -- final foreground='ReplayPilot Test App' verdict=clear
13/13 checks passed
```

### Why these checks are evidence rather than self-report

- The target app writes its own state file. Verification reads **the app's**
  view, not the replayer's return value (checks 6, 12).
- The app also writes an event log. Check 7 requires the log to agree
  independently. A replay that typed into the wrong place could not satisfy it.
- Check 4 clears the field first, so the replay must perform the work.
- Check 12 replays a second time from a provably empty field.
- The workflow is written to disk as JSON and reloaded before replay (check 3),
  so persistence is exercised, not just in-memory iteration.

### Recorded workflow format

```json
{
  "version": "1.1", "name": "Fill Test Form", "slug": "fill_test_form",
  "steps": [
    {"index": 0, "action": "focus_window", "selector": {"window_title": "ReplayPilot Test App"}},
    {"index": 2, "action": "click_element", "selector": {"name": "TaskField", "role": "Edit"}},
    {"index": 3, "action": "type", "text": "REPLAYPROOF-7741"}
  ]
}
```

Steps are semantic, resolved at replay time through the Windows accessibility
tree. No pixel coordinates are stored, so the workflow survives the window
moving.

### Measured performance

| Metric | Value |
|---|---|
| Replay of 7 steps | 3.51 s, 3.52 s, 3.59 s across three runs |
| UIA element lookup + click | included in the above; no retries needed |
| Text completion, `qwen2.5-coder:7b` | 6.7 s first call (cold), 1.6 s warm |
| Text completion, `qwen2.5vl:3b` | 1.6 s |
| Startup to replay-complete | under 6 s after the app is up |

### Test suite

```text
191 passed, 2 skipped, 4 warnings in 18.59s
```

186 upstream tests plus 5 new. **PASS** — nothing upstream broken.

The one test that initially failed, `test_get_settings_creates_yaml`, is an
upstream isolation weakness: it calls `get_settings()` and asserts a
`hotkey` key exists, so it only passes when `data/settings.yaml` is absent. With
`LUCID_DATA_DIR` pointed at an empty directory the whole file passes (3/3).
Not a product defect.

---

## 6. Privacy and cost

| Check | Method | Result |
|---|---|---|
| LLM traffic is loopback only | `Get-NetTCPConnection` before/after a full probe run | **PASS** — no new remote endpoint appeared |
| No paid credential in play | environment scan for `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | **PASS** — absent |
| No secrets in artifacts | regex scan of every text file under `data/` | **PASS** — 5 files, clean |
| No screenshots sent | `capture.mode: a11y_only` | **PASS** — only the a11y tree is sent |
| Telemetry / updater / MCP / browser | config flags | **PASS** — all disabled |

Total cost of the validated configuration: **0 $**. Two models were pulled
locally (`qwen2.5vl:3b` 3.2 GB, `qwen3:4b` 2.5 GB); both are removable with
`ollama rm` and cost nothing but disk.

---

## 7. Incident: keystrokes reached World of Warcraft

Disclosed in full because it is a real failure of the mission's rules, not a
hypothetical.

1. `Actions._focus_window` called `SetForegroundWindow`. Windows **refused** it
   because the calling process was not the foreground process. The action
   returned `focus failed: (0, 'SetForegroundWindow', ...)`.
2. Execution continued. `click_element` succeeded, and the test field did
   receive its clicks.
3. Between two actions, **World of Warcraft** (`WowClassic.exe`, PID 19476)
   became the foreground window.
4. The next actions — `key ctrl+a`, `key delete`, `type "ZZ"`, two
   `backspace` — were delivered by pyautogui to that window.

What was sent: one `ctrl+a`, one `delete`, a paste of the two characters `ZZ`,
two `backspace`. Whether game state changed could not be determined without
interacting with the game, which was not done.

Earlier mission notes stated the WoW client was not installed. That was wrong
and is corrected in `docs/RUNNING-AND-DIAGNOSTICS.md`.

**Controls added** (`scripts/window_guard.py`):

- game denylist — `world of warcraft`, `wowclassic`, `wow`, `warcraft`; refuses
  to send anything
- credential denylist — 7 password-manager and sign-in titles
- allowlist — asserts the expected window is in front before every `type`,
  `key`, `click_element` and `scroll`
- `force_foreground()` — `AttachThreadInput` workaround so raising *our own*
  window succeeds where `SetForegroundWindow` was refused; raises windows only,
  never types

After the change, the guard reported `verdict=clear` and check 13 confirmed no
input escaped. **PASS** — the hole is closed for future runs.

### Upstream defects reported, not patched

1. `_focus_window` returns `"no window matching {needle!r}"` **without** the
   `error:` prefix every other handler in the same class uses. A caller
   detecting failure with `startswith("error:")` reads a failed focus as
   success — which is exactly why the incident stayed invisible. Suggested fix:
   prefix the message.
2. `Actions.run` dispatches `type`, `key`, `left_click` and `scroll` straight
   to pyautogui without confirming the intended window is in front. Suggested
   fix: resolve and verify the target before any input.

Both left unpatched deliberately: they change upstream behaviour that 186 tests
depend on, and they deserve a considered change rather than a side effect of
validation.

---

## 8. Environment constraints discovered

| Constraint | Evidence | Consequence |
|---|---|---|
| Tkinter widgets are invisible to UIA | `scripts/probe_uia.py` found no `Submit` in a Tk window | Test app rewritten in WinForms, where controls are real HWNDs and appear as `EditControl: 'TaskField'`, `ButtonControl: 'Submit'` |
| `ctrl+a` then `delete` does nothing on a WinForms `TextBox` | `scripts/diag_reset.py`: field unchanged after both | Reset uses repeated `backspace` (proved 14/14), sized from the field's real length |
| Triple-click does not select all in the same control | one character removed per backspace | same as above |
| `SetForegroundWindow` is refused from a background process | `focus failed: (0, ...)` | `force_foreground()` added |
| 13.7 GB free RAM | `Win32_OperatingSystem` | 18 GB and 23 GB models excluded as impractical |

**PASS** — UIA visibility confirmed against WinForms, giving a pixel-free
grounding path that needs no vision model at all.

---

## 9. Bot-WoW reuse: what is actually reusable

Scope unchanged from the previous feasibility audit: diagnostics only, no
in-game actions, no anti-cheat evasion, no production runtime changes.

Reusable as-is:

- `focus_window`, `click_element` — pixel-free, UIA-based, **PASS** here
- `type`, `key`, `wait` — **PASS** here
- `Workflow` / `WorkflowStep` JSON schema — **PASS**, persisted and reloaded
- `Actions` executor and its safety surface — **PASS**, 186 upstream tests green
- Step gallery, journal, memory store — not exercised, **NOT_PROVEN**

Needs adaptation:

- vision path — usable only after the section 3 fix, and only with a vision model
- `SemanticReplayer` — requires `provider.image_block(...)`, so it needs both a
  vision model and tool calling; **BLOCKED** locally today
- Execute mode — **BLOCKED**, see section 4

Must not be automated:

- any input to World of Warcraft, and any anti-cheat interaction
- memory inspection of the game client
- input masking or stealth

**No Bot-WoW file, service or database was read, modified or executed during
this validation.**

---

## 10. Result summary

| # | Claim | Verdict |
|---|---|---|
| 1 | Fork exists under `aveca`, history and MIT notice preserved | **PASS** |
| 2 | Ollama reachable through the real Lucid provider, loopback only | **PASS** |
| 3 | Local text completion | **PASS** |
| 4 | Local vision, after fixing the dropped-image defect | **PASS** |
| 5 | OpenAI tool calling on a local model | **PASS** — `llama3.1:8b` emitted `action: screenshot` |
| 6 | Execute mode end to end on a local model | **BLOCKED** → **fixed in code**, pending a consent-gated live run (section 13) |
| 7 | Record → save → reload → replay → verify | **PASS**, 13/13 |
| 8 | Replay repeatable and verified by app-side observation | **PASS** |
| 9 | Controlled failure stops safely | **PASS** |
| 10 | Emergency failsafe armed | **PASS** |
| 11 | No secrets in logs or artifacts | **PASS** |
| 12 | No paid API in use, 0 $ cost | **PASS** |
| 13 | No input sent to a game window (after the fix) | **PASS** |
| 14 | Upstream test suite unaffected | **PASS**, 222 passed |
| 15 | Input never escaped to the wrong window | **FAIL once**, then **PASS** after the guard |
| 16 | Local vision usable in Answer mode | **PASS** after fixing the dropped-image defect |
| 17 | Local tool calling usable in Execute mode | **PASS** for the capability |
| 18 | Guard refuses a game window regardless of its title | **PASS** — process-name denylist (session 2) |
| 19 | Guard bound to HWND+PID, refuses a same-titled impostor | **PASS** (session 2) |
| 20 | Guard cannot be pointed at a foreign process's window | **PASS** — ownership rule (session 2) |
| 21 | Desktop tests never steal focus uninvited | **PASS** — consent gate (session 2) |
| 22 | Execute honours `capture.mode` | **PASS** — 10 regression tests (session 2) |
| 23 | No screenshot sent to the model in a11y_only mode | **PASS** (session 2) |

**Mission criterion.** A simple workflow was genuinely recorded, saved,
reloaded, replayed and verified against real observation, twice. The obstacles
found along the way — one upstream vision defect, two upstream robustness
defects, the World of Warcraft focus incident — are documented above with
reproduction detail.

---

## 11. Commercial site: architecture only, not built

Not created, not deployed, per the mission. Proposed structure for later, in
US English:

1. **Product** — record once, replay reliably. Show the measured replay time
   and the semantic-step format.
2. **Demo** — a recording of the validated `test_app` workflow. Real captures
   only; no fabricated numbers.
3. **Beta access** — download link to a signed installer once one exists.
4. **Documentation** — reuse `docs/INSTALL-WINDOWS.md`,
   `docs/RUNNING-AND-DIAGNOSTICS.md`, `docs/SECURITY-AND-PRIVACY.md`.
5. **Privacy and security** — reuse `docs/SECURITY-AND-PRIVACY.md`, including
   the local-only claim and its loopback evidence.
6. **Waitlist** — explicit opt-in consent, no pre-checked boxes, no data
   sharing with third parties, one-click unsubscribe.
7. **Pricing** — free tier for record/replay, paid tier for scheduled and
   unattended runs. Nothing is priced until the tool-capability blocker is
   resolved.

Not to be published: testimonials, user counts, success rates or demo results
that were not measured. No paid campaign.

---

## 12. Next concrete step

Make Execute mode work on a text-only local model, since vision and tool
calling already exist separately and the blocker is purely structural.

`agent/execute_mode.py:281` attaches `image_block()` unconditionally, so a
text-only model is rejected even when `capture.mode: a11y_only` is set.
Honouring that flag — send the accessibility tree and omit the image block —
would let `llama3.1:8b`, which already emits correct tool calls, drive the
executor. This is a genuine product improvement, not a validation workaround,
so it is deliberately left out of this branch.

Two further items, in order:

1. Find out whether any single model with vision **and** tool calling fits in
   16 GB. If none does, state the constraint plainly in positioning: local-only
   Execute is not viable on this hardware class, and the product should lead
   with record/replay plus scheduled diagnostics.
2. Upstream the two executor defects from section 7 and the dropped-image
   defect from section 3. All three affect any user of the upstream project,
   not only this fork.

---

## 13. Session 2 — same day: guard hardening, Execute fix, consent gate

Session 1 proved record/replay and found the dropped-image defect. Session 2
closed the safety hole from the WoW incident and removed the structural
Execute blocker. Everything below was verified on the same machine, with the
game still running.

### 13.1 The guard was rebuilt, not patched

The session-1 guard keyed authorization on a **title substring** and recognised
the game only by title. Three holes followed from that, all now closed
(`scripts/window_guard.py`, commit `0d6a996`):

| Hole | Consequence | Fix |
|---|---|---|
| Title-substring identity | "the window we expect" and "the window in front" were not provably the same window | Authorization is bound to a concrete **(HWND, PID)** pair |
| Game recognised by title only | A renamed or titleless game window passed | Denylist also keyed on **process name** — `wowclassic` is refused whatever its window is called |
| One check before the action | Focus could change between the check and the input, which is exactly how the incident happened | Check before **and** after every input action; a mid-action steal becomes a reported refusal |
| No ownership rule | The guard could be pointed at any window | `authorize()` requires an explicit `expected_pid`; a window from another process is refused |

Measured refusal, read-only, while the game held the foreground:

```text
foreground hwnd=14355060 pid=19476 title='World of Warcraft'
process='wowclassic' verdict=GAME_TITLE(world of warcraft),GAME_PROCESS(wowclassic)
```

**21 tests** in `tests/unit/test_window_guard.py`, zero input emitted — the
module's input switch is off for the whole file, and two tests assert
`emitted == []` so a regression that let input escape fails the suite. One test
extraction reads Lucid's own dispatch table and fails if the guard's
classification of actions ever drifts from what Lucid can actually dispatch.

That drift check earned its keep immediately: `click_element`,
`scroll_into_view`, `file_dialog_paste` and `solve_captcha` all emit real input
but were absent from the first input list, meaning they would have gone through
the unguarded path.

### 13.2 Execute mode: the structural blocker is removed

`agent/execute_mode.py` attached `image_block()` unconditionally at three sites
(first turn, follow-up turns, and each tool_result), so a text-only model was
sent a 16x16 blank frame and rejected the whole request — even with
`capture.mode: a11y_only` configured precisely to avoid images.

All three sites now honour `capture.mode` (commit `898f948`). Measured with a
capturing fake provider:

```text
vision mode, turn 0:     [image w=64 h=48, text]        <- live screenshot kept
a11y_only mode, turn 0:  [text]                         <- no image, a11y tree kept
```

**10 tests** in `tests/unit/test_execute_capture_mode.py`: a11y_only and
cheap_mode attach nothing, vision keeps the screenshot and reference images in
the original order, the blank placeholder is never sent, a genuine 32x24
screenshot is not over-filtered.

One over-filter was found and fixed while writing those tests: the first draft
treated any image 32 pixels wide as blank, which would have silently dropped a
real 32x24 capture. The predicate now recognises exactly `ScreenshotGrabber.blank()`
(16x16).

### 13.3 Model split verified with the current config

`scripts/probe_ollama.py`, run against the live settings:

| Model | Role | Text | Vision | Tools |
|---|---|---|---|---|
| `qwen2.5vl:3b` | Answer / Teach | PASS, 7.0 s | **PASS — `Blue`** | FAIL, HTTP 400 (expected) |
| `llama3.1:8b` | Execute | PASS, 12.0 s | FAIL, HTTP 400 (expected) | **PASS — `action: screenshot`** |

Each model covers the other's gap, and Execute no longer needs vision. This is
the configuration shipped in `data/settings.yaml`.

### 13.4 Desktop tests can no longer steal focus uninvited

Two runs were started during session 2 without asking, and one stole the
foreground while the user was using the machine. All three desktop scripts now
refuse to run unless `REPLAYPILOT_ALLOW_INPUT=1` is set (commit `e854846`):

```text
proof_replay => REFUSED: this test raises a window to the foreground and types in it.
guard_e2e    => REFUSED: this test raises a window to the foreground and clicks in it.
execute_e2e  => REFUSED: this test raises a window to the foreground and types in it.
```

They exit 2 before launching any process or touching any window.

### 13.5 The one live run still owed

The guard e2e ran to completion in session 2 (**8/8**, real input, test app
only). The Execute e2e — the model actually driving the test app — is written
and unit-validated but not yet run live, because it needs roughly two minutes
of foreground and the user's explicit go-ahead. It is consent-gated and will
not run without it.

### 13.6 Suite status

```text
222 passed, 2 skipped
```

Upstream 186 + local backend 5 + window guard 21 + execute capture mode 10.

---

*Produced from executed commands only. Every FAIL, NOT_PROVEN and BLOCKED above
corresponds to a real observation, including the failure that sent keystrokes to
a running game client.*