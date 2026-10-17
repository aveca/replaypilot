# Running and diagnosing ReplayPilot

Short operational reference: how to start it, how to tell what is wrong, and
how to read the artifacts it leaves behind.

---

## 1. Start

```powershell
cd C:\replaypilot
.venv\Scripts\python.exe -m lucid
```

`ctrl+alt+j` opens the Spotlight overlay. Press it once, type a prompt, and
pick a mode with `ctrl+1` (Answer), `ctrl+2` (Teach), `ctrl+3` (Execute), or
cycle with `Tab`.

Answer and Teach work with a text-only local model. Execute needs a model that
accepts images **and** emits OpenAI tool calls — see *Model selection* in
`docs/INSTALL-WINDOWS.md`.

---

## 2. Emergency stops

| Action | How | Notes |
|---|---|---|
| Kill switch | `ctrl+shift+k` | Configurable via `safety.kill_switch_hotkey` |
| Corner abort | move pointer to the top-left corner | `safety.failsafe`, armed as `pyautogui.FAILSAFE` |
| Undo on stop | `auto_undo_on_stop: false` by default | When enabled, sends `ctrl+z` to the foreground window on stop |
| Destructive prompts | modal Deny / Allow / Always | `safety.destructive_confirm: true` |
| Scheduled destructive steps | auto-denied when headless | No unattended deletion |

---

## 3. Diagnostics

### 3.1 Built-in doctor

```powershell
.venv\Scripts\python.exe -m lucid doctor
```

Checks: backend reachability, hotkey availability, DPI awareness, write
permissions to `data/`, model resolution. All five pass in the validated
configuration.

### 3.2 LLM compatibility probe

```powershell
.venv\Scripts\python.exe scripts\probe_ollama.py
```

Reports four things, each classified PASS / FAIL / NOT_PROVEN:

1. provider registry loads,
2. plain text completion against the local model,
3. **vision capability** — sends a flat blue square and checks the answer is
   not a refusal. A text-only model answers politely and still fails this,
   which is why the probe does not treat HTTP success as vision support,
4. **OpenAI tool calling** — this is what Execute mode depends on.

At the end it lists every installed model with a `vision=` marker so the choice
is documented rather than guessed.

### 3.3 Replay proof

```powershell
.venv\Scripts\python.exe scripts\proof_replay.py
```

Records a workflow against a harmless local test app, saves it, reloads it,
replays it, and verifies the outcome by reading the app's own state file. It
also checks safe failure, the failsafe, secret hygiene, absence of paid
credentials, repeatability, and that the focus guard held.

Run it after any change to the executor, the recorder, or the local backend.
It is the regression test for the product's core claim.

### 3.4 Focus guard

```powershell
.venv\Scripts\python.exe scripts\window_guard.py
```

Prints the foreground window and whether the guard considers it safe. Useful
before any manual automation session.

```text
foreground='World of Warcraft' verdict=GAME(world of warcraft),GAME(warcraft)
```

means: refuse to type.

---

## 4. Artifacts

Everything lives under `data/`, git-ignored, movable as one folder.

| Path | Contents |
|---|---|
| `data/settings.yaml` | configuration, including the Ollama endpoint |
| `data/memory.db` | SQLite: facts, files, task patterns (BM25 retrieval) |
| `data/workflows/*.json` | recorded workflows, one file per slug |
| `data/journals/` | per-session JSONL plus WebP step thumbnails |
| `data/screenshots/` | persisted captures |
| `data/logs/` | runtime logs |
| `data/profile.yaml` | user profile; **never commit** real values |

A recorded workflow is plain JSON and readable by hand:

```json
{
  "version": "1.1",
  "name": "Fill Test Form",
  "slug": "fill_test_form",
  "steps": [
    {"index": 0, "action": "focus_window", "selector": {"window_title": "ReplayPilot Test App"}},
    {"index": 2, "action": "click_element", "selector": {"name": "TaskField", "role": "Edit"}},
    {"index": 3, "action": "type", "text": "REPLAYPROOF-7741"}
  ]
}
```

Steps are semantic, not pixel coordinates. That is what allows a replay to
re-resolve a control after the window moves.

---

## 5. Common problems

### "openai package required for lm_studio backend"

`openai` is not declared upstream. Install it explicitly:

```powershell
uv pip install -e ".[dev]" openai
```

### "LM Studio reported no loaded models"

That message also covers Ollama. Check:

```powershell
Invoke-RestMethod http://localhost:11434/v1/models
```

If empty, Ollama is not running or has no model pulled.

### Answer works but Execute does nothing

The model is not emitting tool calls. `probe_ollama.py` check 4 confirms it.
Text-only coder models generally do not. This is a model capability limit, not
a configuration error.

### A typed action went somewhere unexpected

Read section 3 of `docs/SECURITY-AND-PRIVACY.md`. The short version: check
`focus_window` is succeeding, and run scripts under `window_guard.py` so input
is refused when the wrong window is in front.

### "Ctrl+A then Delete does nothing" in a recorded workflow

Measured behaviour, not a bug in your setup: on a WinForms `TextBox`,
`key ctrl+a` followed by `key delete` had no effect, and triple-click did not
select all either. Repeated `key backspace` removes exactly one character per
call and is the reliable way to clear a field. Size the loop from the field's
actual length.

### `focus failed: (0, 'SetForegroundWindow', ...)`

Windows refuses `SetForegroundWindow` when the calling process is not the
foreground process. Use the `AttachThreadInput` approach in
`scripts/window_guard.py::force_foreground`.

---

## 6. Fact versus assumption

Two claims in earlier mission notes were wrong and are corrected here:

- **"WoW client not installed on this machine"** — incorrect. `WowClassic.exe`
  was running during validation, PID 19476. Never run unguarded automation on
  a machine with a game client open.
- **"Lucid has a built-in Ollama backend"** — incorrect. There is no `ollama`
  mode. `backend.mode` accepts `api`, `cli`, `lm_studio`. Ollama is reached
  through `lm_studio` by pointing `lm_studio_url` at `http://localhost:11434/v1`.

Both corrections changed what the validation harness does, and both are
reflected in the code.