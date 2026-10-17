# Installing ReplayPilot on Windows

ReplayPilot is a fork of [Lucid](https://github.com/ertugrulakben/lucid) (MIT)
configured to run against a **local Ollama** model. Nothing is sent to a hosted
API and no paid API key is needed.

- License: MIT (see `LICENSE`). Lucid's copyright notice is preserved.
- Tested on: Windows 11, Python 3.11.9, 2560x1440 interactive session.

---

## 1. Prerequisites

| Requirement | Detail |
|---|---|
| OS | Windows 10 or 11 (UIA + Win32 window enumeration are required) |
| Python | 3.10+ (validated on 3.11.9) |
| [uv](https://docs.astral.sh/uv/) | optional but recommended, much faster installs |
| [Ollama](https://ollama.com/) | runs the model locally |
| RAM | 16 GB recommended. A 3B model is comfortable at 8 GB; a 30B model was measured as impractical here (see *Model selection*) |

Verify Ollama is up:

```powershell
ollama list
Invoke-RestMethod http://localhost:11434/v1/models | Select-Object -ExpandProperty data
```

The second command is the important one. Lucid talks to the **OpenAI-compatible**
endpoint at `http://localhost:11434/v1`, not to Ollama's native `/api`.

---

## 2. Install

```powershell
git clone https://github.com/aveca/replaypilot.git
cd replaypilot

# isolated venv, nothing global is touched
uv venv --python 3.11 .venv

# NOTE: `openai` is required by the local backend but is NOT declared in
# upstream pyproject.toml. It must be requested explicitly.
uv pip install -e ".[dev]" openai
```

> **Upstream gap.** `lm_studio_backend.py` does `from openai import OpenAI`
> and raises `RuntimeError("openai package required...")` at runtime, yet
> `openai` appears in neither `dependencies` nor any extra. Installing
> without it fails only when the first request is made. Installing it
> explicitly, as above, avoids that.

---

## 3. Configure

`data/settings.yaml` ships with this fork already pointed at Ollama:

```yaml
provider: lm_studio
model: qwen2.5-coder:7b
execute_model: qwen2.5-coder:7b
hotkey: "ctrl+alt+j"

backend:
  mode: lm_studio
  lm_studio_url: http://localhost:11434/v1
  lm_studio_model: qwen2.5-coder:7b
  lm_studio_api_key: ollama-local   # placeholder, Ollama ignores it

capture:
  mode: a11y_only                   # see below
```

There is **no `ollama` mode**. `create_backend()` and the provider registry
only accept `anthropic`, `api`, `cli` and `lm_studio`. Ollama is reached through
`lm_studio`, whose own docstring states it works with "LM Studio's OpenAI
adapter, so this backend works for both with a URL swap."

### `capture.mode`

| Value | Behaviour | Needs a vision model? |
|---|---|---|
| `vision` | always sends a screenshot | yes |
| `a11y_only` | sends only the Windows accessibility tree | no |
| `hybrid` | a11y first, screenshot when sparse | only sometimes |

`a11y_only` is the default here because the installed models are text-only.
Switch to `vision` only after installing a vision model.

---

## 4. Model selection

Measured on this machine (31.6 GB RAM, 13.7 GB free at the time):

| Model | Size | Vision | Tools | Verdict |
|---|---|---|---|---|
| `qwen2.5-coder:7b` | 4.7 GB | no | no | text work only |
| `llama3:8b` | 4.7 GB | no | HTTP 400 on `tools` | unusable for Execute |
| `qwen2.5-coder:32k` | 4.7 GB | no | no | same family as above |
| `qwen2.5-coder-7b-16k` | 4.7 GB | no | no | same model, longer context |
| `qwen3-coder:30b` | 18 GB | no | untested | **impractical**: 18 GB model vs 13.7 GB free |
| `qwen3.6:latest` | 23 GB | no | untested | **impractical**: same reason |

**Consequence.** Execute mode (the model driving the mouse and keyboard) needs
*both* image input and OpenAI function calling. No installed model provided
both. Use a coder model for Answer/Teach text work, and install a small vision
model before expecting vision behaviour. Keep it small: a 3B vision model fits
comfortably; a 30B model does not fit this machine's free memory.

---

## 5. Verify the install

```powershell
.venv\Scripts\python.exe -m lucid doctor
```

Expected:

```
[     OK]  API key configured  -- backend=lm_studio at http://localhost:11434/v1
[     OK]  Hotkey not in use by another app  -- `ctrl+alt+j` is free
[     OK]  Display scaling detected  -- DPI awareness level 2
[     OK]  Required permissions granted  -- writable: ...\replaypilot\data
[     OK]  Backend can reach the configured model
All checks passed.
```

Then run the two harnesses:

```powershell
.venv\Scripts\python.exe scripts\probe_ollama.py     # LLM compatibility
.venv\Scripts\python.exe scripts\proof_replay.py     # record -> replay -> verify
```

`proof_replay.py` types into a harmless local test app only. It refuses to run
if a game or credential window owns the foreground; see
`docs/SECURITY-AND-PRIVACY.md`.

---

## 6. Run the app

```powershell
.venv\Scripts\python.exe -m lucid
```

The Spotlight overlay opens on `ctrl+alt+j`. `ctrl+shift+k` is the kill switch.
Moving the mouse to the top-left screen corner aborts immediately
(`safety.failsafe`, armed through `pyautogui.FAILSAFE`).

---

## 7. Uninstall

Nothing is written outside the project folder except the Ollama model cache.

```powershell
Remove-Item -Recurse -Force .venv
git checkout -- .          # discard config edits
```

Models live in the Ollama cache and can be removed with `ollama rm <model>`.