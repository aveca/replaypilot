"""Mode C: hand over mouse/keyboard to the LLM via a custom ``computer`` tool.

This module is the bone of Lucid's autonomy. The loop:

    snapshot + goal  →  Claude plans  →  Claude emits a tool_use  →
      SafetyGuard + RetryBudget + verify.snapshot() (before)  →
        Actions.run()  →  verify.snapshot() (after) + diff  →
          fresh ContextSnapshot  →  tool_result (text + image)  →
            decorate_result (escalate if no change)  →  next turn

Surrounding infrastructure (added in v0.3):
- **Memory retrieval**: past task patterns + facts are injected into the
  first user turn so Claude starts with hard-won context ("last time you
  solved X in Y, here's what worked").
- **Profile injection**: the user's ``profile.yaml`` is summarised and added
  so the LLM knows the operator without anything being hard-coded.
- **Follow-up freshness**: every follow-up re-captures the desktop *now*
  instead of re-using the stale snapshot the caller passed in (old bug).
- **Per-action timeout**: ``executor.step_timeout_seconds`` now actually
  applies, wrapped around each tool invocation.
- **Captcha detector + solver**: every post-action snapshot is scanned; an
  obvious captcha element triggers the ``CaptchaSolver`` (rate-limited).
- **Fact recorder**: at the end of a successful run we optionally extract
  1-3 one-sentence lessons into ``memory.db`` for future prompts.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator

from lucid.capture import ContextSnapshot
from lucid.config.profile import Profile, pick_profile_snippet
from lucid.config.settings import Settings
from lucid.executor import (
    Actions,
    RetryBudget,
    SafetyGuard,
    decorate_result,
    install_kill_switch,
)
from lucid.executor import (
    snapshot as state_snapshot,
)
from lucid.executor.verify import diff as state_diff
from lucid.journal import StepJournal
from lucid.llm.anthropic_client import build_computer_tool
from lucid.llm.provider import LLMProvider, Message
from lucid.llm.schemas import ActionBlock
from lucid.memory.recorder import FactRecorder
from lucid.memory.retrieval import context_block as memory_context_block
from lucid.memory.store import MemoryStore
from lucid.recorder.registry import WorkflowEntry, WorkflowRegistry
from lucid.recorder.workflow import load_workflow
from lucid.replayer.semantic_replay import SemanticReplayer
from lucid.safety.captcha import CaptchaSolver, detect_captcha

log = logging.getLogger("lucid.agent.execute")

SYSTEM_PROMPT = (
    "You are Lucid in autonomous mode. You control the user's mouse and keyboard "
    "through the `computer` tool. The user provides a goal and an initial "
    "screenshot. Plan silently, act one step at a time, observe the new "
    "screenshot after every action, and stop as soon as the goal is satisfied. "
    "Ask for confirmation before destructive actions (delete, overwrite, send). "
    "Output short progress messages.\n\n"
    "PREFER keyboard and SEMANTIC actions OVER pixel clicks. Guessing pixel "
    "coordinates from a downscaled screenshot is unreliable even with the "
    "coordinate mapping. Always try these deterministic patterns first:\n"
    "- Click a LABELED button/menu/link: `click_element` with `element_name` "
    "(substring of the visible label, e.g. 'Sign in', 'Close', 'Send'). "
    "Optional `element_role` ('Button', 'MenuItem', 'Hyperlink'). "
    "Matching is case-insensitive and language-agnostic — use whatever label is "
    "actually on screen.\n"
    "- Switch to an existing window: `focus_window` with `window_title` "
    "(distinctive substring). Far more reliable than Alt+Tab.\n"
    "- Launch an app: `key` ['win','r'], `type` program name, `key` ['return'].\n"
    "- Focus browser address bar: `key` ['ctrl','l']  then `type` URL, then "
    "`key` ['return'].\n"
    "- Jump to Chrome tab N: `key` ['ctrl','<N>'] (1..8; ['ctrl','9'] = last).\n"
    "- New/close tab: `key` ['ctrl','t'] / ['ctrl','w'].\n"
    "- Save / find: `key` ['ctrl','s'] / ['ctrl','f'].\n"
    "- Select / copy / paste: `key` ['ctrl','a'] / ['ctrl','c'] / ['ctrl','v'].\n"
    "- Native File Open/Save dialog: emit `file_dialog_paste` with `file_path` "
    "set to the absolute path. Lucid focuses the filename field, pastes the "
    "path, and presses Enter in one shot. Don't navigate folder trees manually.\n"
    "- Bring a labelled element into view before clicking: `scroll_into_view` "
    "with `element_name`.\n"
    "- Solve a captcha when the detector flags one: `solve_captcha`.\n"
    "PIXEL CLICKS ARE THE LAST RESORT — not the first option. A pixel coordinate "
    "is only acceptable when (a) you tried click_element on the labelled "
    "element and it returned not-found, OR (b) the target is unlabelled "
    "(canvas, image, game, custom-drawn widget). If you emit a coordinate-based "
    "left_click/right_click, the `reason` field MUST begin with 'FALLBACK: ' "
    "and explain why semantic/keyboard paths were unavailable. Reviewers will "
    "reject any coord click whose reason doesn't start with that prefix.\n\n"
    "NATIVE SCREENSHOT — prefer `screenshot_to_clipboard` over `PrintScreen` / "
    "`Win+Shift+S`. The native action captures the current screen (or a named "
    "monitor) directly into the Windows clipboard as a PNG, without opening "
    "Snipping Tool. No UI flicker, no race condition, no 'did the snip work?' "
    "ambiguity. Use PrintScreen only as last-resort fallback.\n\n"
    "SHELL PEEK — when you need to know 'where is a file' / 'is a process "
    "running' / 'what does this folder contain', use `run_shell` with a short "
    "read-only command (`dir`, `Get-Process`, `Get-ChildItem`, `where.exe`, "
    "`type`, `findstr`). NEVER open a terminal window just to check something.\n\n"
    "DO NOT call the `screenshot` action. Every other action automatically "
    "returns a fresh screenshot in its tool result.\n\n"
    "IF A CLICK FAILS TWICE AT THE SAME COORDINATE, AT THE SAME ELEMENT NAME, "
    "OR WITH THE SAME KEY COMBO, STOP retrying. Retry budget exhaustion "
    "surfaces as '[retry-guard]' in the tool_result. When you see it, pick a "
    "completely different approach (keyboard shortcut, focus_window, "
    "click_element with a different name) — never repeat the same failed "
    "action. Looping on the same key combo (Win+Shift+S four times in a row) "
    "is always wrong.\n\n"
    "PRESERVE THE USER'S STATE. Never close existing tabs, windows, or "
    "documents that are not part of your goal. In a browser, open a NEW tab "
    "with `key` ['ctrl','t'] and work there; don't reuse existing tabs, don't "
    "press Ctrl+W on ones you didn't open.\n\n"
    "FORMS AND DIALOGS - USE TAB TO NAVIGATE. In Gmail compose, signup forms, "
    "Windows dialogs, `key` ['tab'] moves to the next field reliably. Do NOT "
    "click field after field.\n"
    "- Gmail: after To is filled, Tab → Subject, Tab → Body. Attach via "
    "`key` ['ctrl','shift','a'] (opens Attach dialog, then emit "
    "`file_dialog_paste`).\n"
    "- Submit messaging/email: `key` ['ctrl','return'].\n\n"
    "FOLLOW-UP INSTRUCTIONS. When a message is labelled 'Additional "
    "instruction from the user', continue the SAME task from the current "
    "screen state. Do NOT restart from scratch, do NOT ask for context that "
    "was already given.\n\n"
    "MULTI-STEP / LONG TASKS. When the user lists several sub-goals in one "
    "prompt ('do X, then Y, then Z' or a '[LONG-TASK MODE]' prefix), treat "
    "each sub-goal as a named checkpoint and execute them in order. DO NOT "
    "emit [done] after finishing a single sub-goal — only after every "
    "sub-goal has been verified complete. Every 10-15 steps, write a short "
    "progress line such as 'checkpoint 2/5: Gmail compose opened'. If a "
    "sub-goal becomes blocked, report which one and why, then move on to "
    "the next independent sub-goal rather than stopping the whole run.\n\n"
    "ACCESSIBILITY TREE: Each screenshot comes with a flat a11y listing "
    "(role | name | cx,cy). Prefer those named centres over pixel guesses.\n\n"
    "LUCID PANEL: A small dark progress panel may be docked in a corner "
    "(~700x300 px) showing your own output. NEVER click or type into it. "
    "Always act on the main workspace.\n\n"
    "MULTI-MONITOR. The context block lists every physical display with its "
    "index and absolute bounds (see 'Monitors (N total)' in the context). "
    "Each snapshot shows ONE monitor only — the one currently under the "
    "cursor. When the user references a different screen ('on the right "
    "monitor', 'sağ ekranda', 'on screen 2', 'on the primary'), emit "
    "`focus_monitor` with either `index` or `position` (primary / left / "
    "right / above / below) BEFORE trying to click anything. The next "
    "snapshot will show that monitor. Never guess a pixel on a monitor you "
    "haven't captured — the coordinate would land on a different display.\n\n"
    "UPSTREAM ASSISTANT. Some users run a separate larger LLM in VS Code "
    "(e.g. Claude Code) that handles email, calendar, and long-horizon "
    'projects. When the user says things like "tell my assistant I '
    'finished", "leave a note for the other agent", or "mail myself when '
    'done", do NOT try to guess at some chat box; instead:\n'
    "  (a) If Gmail is the clearest path, open https://mail.google.com in "
    "      a new Chrome tab, compose an email TO the user's own address "
    "      (see User profile → Email) with a concise status body.\n"
    "  (b) If the user explicitly asks to open VS Code, use focus_window on "
    "      'Visual Studio Code' (or launch it via Win+R → 'code'), then "
    "      continue per the user's instruction.\n"
    "  (c) If neither, fall back to `screenshot_to_clipboard` plus a clear "
    "      [done] line summarising what was finished.\n"
    "Never assume a private chat with another agent exists inside the "
    "Lucid overlay — the overlay is the user's prompt line, not a bridge."
)


class ExecuteMode:
    def __init__(
        self,
        settings: Settings,
        provider: LLMProvider,
        memory_store: MemoryStore | None = None,
        profile: Profile | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.store = memory_store
        self.profile = profile
        self.actions = Actions(settings, memory_store=memory_store)
        self.safety = SafetyGuard(settings)
        self._cancel = threading.Event()
        self._kill_switch_unreg = None
        self._captcha = (
            CaptchaSolver(settings, memory_store, provider, self.actions) if memory_store else None
        )
        self._fact_recorder = (
            FactRecorder(memory_store, provider, settings) if memory_store else None
        )
        # Persistent conversation across Execute dispatches so follow-up
        # instructions ("now attach the file") continue the prior task.
        self._messages: list[Message] = []
        self._current_goal: str = ""
        self._target_app: str | None = None
        self._transcript: list[str] = []
        self._journal: StepJournal | None = None

    def cancel(self) -> None:
        self._cancel.set()

    def reset(self) -> None:
        """Drop the running Execute conversation (called by New Conversation)."""
        self._messages = []
        self._transcript.clear()
        self._current_goal = ""
        self._target_app = None
        self._journal = None
        # Tear down the shared Playwright runtime, if any, so a new
        # conversation starts with a fresh browser context instead of
        # inheriting the previous task's cookies and open tabs.
        try:
            from lucid.actions.browser.runtime import BrowserRuntime

            BrowserRuntime.reset()
        except Exception as exc:
            log.debug("browser reset skipped: %s", exc)

    def run(
        self,
        prompt: str,
        snapshot: ContextSnapshot,
        cancel: threading.Event,
        attachments: list | None = None,
    ) -> Iterator[str]:
        """Execute ``prompt`` against the live desktop.

        ``attachments`` accepts a list of PIL ``Image`` objects the user
        wants Lucid to treat as visual context: reference screenshots,
        design mockups, pasted photos. They ride alongside the live
        desktop snapshot in the first user turn ("make it look like this").
        """
        self._cancel = cancel
        self._kill_switch_unreg = install_kill_switch(
            self.settings.safety.kill_switch_hotkey, cancel
        )
        self._transcript.clear()
        self._pending_attachments = list(attachments or [])
        budget = RetryBudget(max_attempts=self.settings.executor.retry_max_attempts)
        self._open_journal_if_enabled(prompt)

        # Before spinning up a fresh Execute loop, check whether the prompt
        # matches a saved workflow. Single-line matches go through the
        # SemanticReplayer (and variable extractor below) which is faster
        # and cheaper than re-planning from scratch.
        try:
            routed = self._maybe_route_to_workflow(prompt, snapshot, cancel)
        except Exception as exc:
            log.debug("workflow routing failed: %s", exc)
            routed = None

        try:
            if routed is not None:
                for chunk in routed:
                    yield chunk
                return
            yield from self._loop(prompt, snapshot, budget)
            yield from self._final_proof(snapshot)
        finally:
            self._pending_attachments = []
            if self._kill_switch_unreg is not None:
                self._kill_switch_unreg()
                self._kill_switch_unreg = None
            self._record_if_enabled(budget)

    def _may_attach_image(self, snapshot: ContextSnapshot) -> bool:
        """Whether a screenshot may be sent to the model on this turn.

        ``capture.mode`` is honoured here, and this is the only place in
        Execute mode that decides it. Previously every turn did
        ``[self.provider.image_block(snapshot.image)]`` unconditionally, so a
        text-only local model was asked to handle a blank image and rejected
        the whole request with HTTP 400:

            Multimodal data provided, but model does not support multimodal
            requests.

        even when ``capture.mode: a11y_only`` had been configured precisely to
        avoid sending images. The accessibility tree is already included in
        the prompt text by ``_initial_user_text``, so a text-only model has
        everything it needs when we skip the image.

        Returns True for ``vision`` mode, for ``hybrid`` when the snapshot
        actually captured something, and False for ``a11y_only``,
        ``capture.cheap_mode``, or a blank screenshot.
        """
        capture_cfg = getattr(self.settings, "capture", None)
        mode = (getattr(capture_cfg, "mode", "vision") or "vision").lower()
        if bool(getattr(capture_cfg, "cheap_mode", False)):
            return False
        if mode == "a11y_only":
            return False
        image = getattr(snapshot, "image", None)
        if image is None:
            return False
        # ScreenshotGrabber.blank() returns a 16x16 black frame when a capture
        # was skipped. Attaching it costs tokens and trips text-only models, so
        # treat exactly that placeholder as "no image".
        if (getattr(image, "width", 0), getattr(image, "height", 0)) == (16, 16):
            return False
        return True

    def _loop(
        self,
        prompt: str,
        stale_snapshot: ContextSnapshot,
        budget: RetryBudget,
    ) -> Iterator[str]:
        attachments = getattr(self, "_pending_attachments", []) or []

        if not self._messages:
            self._current_goal = prompt
            snapshot = stale_snapshot
            self._target_app = _snapshot_app(snapshot)
            content: list = []
            if self._may_attach_image(snapshot):
                content.append(self.provider.image_block(snapshot.image))
                for ref_img in attachments:
                    content.append(self.provider.image_block(ref_img))
            content.append(
                self.provider.text_block(
                    self._initial_user_text(prompt, snapshot, attachment_count=len(attachments))
                )
            )
            self._messages.append(Message(role="user", content=content))
        else:
            # Follow-up: re-capture the desktop NOW (old code reused a stale
            # snapshot captured before the user's new prompt).
            snapshot = ContextSnapshot.capture(self.settings)
            follow_up: list = []
            if self._may_attach_image(snapshot):
                follow_up.append(self.provider.image_block(snapshot.image))
                for ref_img in attachments:
                    follow_up.append(self.provider.image_block(ref_img))
            attach_note = (
                f"\n\n[{len(attachments)} reference image(s) attached — match "
                "their intent on the current desktop.]"
                if attachments
                else ""
            )
            follow_up.append(
                self.provider.text_block(
                    "Additional instruction from the user (continue the prior "
                    f"task, do NOT restart):\n{prompt}{attach_note}\n\n"
                    f"Current screen context:\n{snapshot.to_prompt_context()}\n\n"
                    f"Accessibility tree:\n{_flatten_a11y(snapshot.a11y_tree)}"
                )
            )
            if self._messages[-1].role == "user":
                # Stale tool_result at the end (prior run cancelled).
                self._messages[-1].content.extend(follow_up)
            else:
                # Last message is assistant. If it has unresolved tool_use
                # blocks (prior run interrupted before tool_result), the
                # Anthropic API will reject the next request unless every
                # tool_use id has a matching tool_result. Synthesize
                # placeholder tool_results so the conversation stays valid.
                orphan_ids: list[str] = []
                for block in self._messages[-1].content or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tid = block.get("id")
                        if tid:
                            orphan_ids.append(tid)
                synthetic_results = [
                    self.provider.tool_result_block(
                        tid,
                        "[run interrupted — no result captured. Continue from "
                        "the current screen state shown below.]",
                        is_error=True,
                    )
                    for tid in orphan_ids
                ]
                self._messages.append(Message(role="user", content=synthetic_results + follow_up))

        messages = self._messages
        tool = build_computer_tool(snapshot.image.width, snapshot.image.height)

        steps = 0
        max_steps = self.settings.executor.max_steps
        step_timeout = float(self.settings.executor.step_timeout_seconds or 30)
        empty_action_nudges = 0

        while steps < max_steps and not self._cancel.is_set():
            steps += 1
            assistant_blocks: list[dict] = []
            tool_uses: list[ActionBlock] = []
            last_stop_reason: str | None = None

            _prune_old_images(messages, keep_last=2)

            narration_buffer = ""
            for event in self.provider.stream(
                messages,
                system=self._system_prompt(),
                tools=[tool],
                max_tokens=2048,
                model=self.settings.execute_model,
                cache_system=True,
                cache_tools=True,
            ):
                if self._cancel.is_set():
                    return
                if event.kind == "text_delta":
                    yield event.text
                    self._transcript.append(event.text)
                    assistant_blocks.append({"type": "text", "text": event.text})
                    narration_buffer += event.text
                elif event.kind == "tool_use" and event.tool_use is not None:
                    block = ActionBlock.from_tool_use(event.tool_use)
                    tool_uses.append(block)
                    assistant_blocks.append(
                        {
                            "type": "tool_use",
                            "id": event.tool_use.id,
                            "name": event.tool_use.raw.get("name", "computer"),
                            "input": event.tool_use.raw.get("input", {}),
                        }
                    )
                    # ThoughtChain: flush any accumulated narration first so it
                    # sits above the plan line, then emit the structured plan.
                    if narration_buffer.strip():
                        yield _emit_thought(narration_buffer.strip())
                        narration_buffer = ""
                    yield _emit_thought("🛠 plan: " + _fmt_plan(block))
                elif event.kind == "done":
                    last_stop_reason = event.stop_reason
                elif event.kind == "error":
                    yield f"\n[error] {event.error}\n"
                    self._transcript.append(f"[error] {event.error}")
                    return
            if narration_buffer.strip():
                yield _emit_thought(narration_buffer.strip())
                narration_buffer = ""

            if assistant_blocks:
                messages.append(Message(role="assistant", content=_merge_text(assistant_blocks)))

            if not tool_uses:
                # No action emitted. Two cases:
                # (a) The model genuinely ended the turn — done.
                # (b) The model narrated and ran out of tokens or got distracted.
                # Real "done" only when stop_reason is end_turn (clean stop)
                # AND there is no further work the user asked for. We can't
                # know the latter, so accept end_turn as done after up to 2
                # nudges, and any other stop_reason gets nudged immediately.
                if last_stop_reason == "end_turn" and empty_action_nudges >= 2:
                    yield "\n[done]\n"
                    self._transcript.append("[done]")
                    return
                if empty_action_nudges >= 4:
                    yield "\n[done] (no further actions after nudges)\n"
                    self._transcript.append("[done] no-action-after-nudges")
                    return
                empty_action_nudges += 1
                nudge_text = (
                    "You did not emit a `computer` tool action this turn. "
                    "Look at the latest screenshot and emit ONE concrete "
                    "action to continue the task. Do not narrate further "
                    "until you have made progress. If the task is fully "
                    "complete and verified, simply emit no action and the "
                    "session will end."
                )
                messages.append(
                    Message(
                        role="user",
                        content=[self.provider.text_block(nudge_text)],
                    )
                )
                yield f"\n[nudge {empty_action_nudges}/4] no action emitted, asking model to continue\n"
                continue

            empty_action_nudges = 0

            tool_results: list[dict] = []
            captcha_hit: str | None = None
            for action in tool_uses:
                if self._cancel.is_set():
                    return
                action = _translate_coords(action, snapshot)
                # Snapshot the screen image BEFORE the action so the Step Journal
                # can show a true visual before/after pair, not just the focus diff.
                journal_before_image = snapshot.image if self._journal is not None else None

                decision = self.safety.evaluate(action)
                if decision.requires_confirm:
                    user_answer = self.safety.ask_user(action, decision.reason)
                    if user_answer is None:
                        # No UI broker (headless run): default to denial to stay safe.
                        yield f"\n[needs confirm] {decision.reason}. Skipping (headless).\n"
                        tool_results.append(
                            self.provider.tool_result_block(
                                action.id,
                                f"User did not confirm destructive action: {decision.reason}",
                                is_error=True,
                            )
                        )
                        continue
                    if not user_answer:
                        yield f"\n[denied by user] {decision.reason}\n"
                        tool_results.append(
                            self.provider.tool_result_block(
                                action.id,
                                f"User denied destructive action: {decision.reason}",
                                is_error=True,
                            )
                        )
                        continue
                    yield f"\n[approved by user] {decision.reason}\n"

                # Snapshot screen state BEFORE action so we can diff afterward.
                before = state_snapshot()
                budget.register(action.action, action.params)

                # Per-action timeout — guard against a hung tool invocation.
                result_text = self._run_action_with_timeout(action, step_timeout)

                after = state_snapshot()
                delta = state_diff(before, after)
                if delta is None and action.action not in {"wait", "screenshot"}:
                    budget.mark_no_effect()
                else:
                    budget.mark_effective()

                # Annotate tool_result so Claude sees retry/noop guidance.
                annotated = decorate_result(result_text, budget, action.action, action.params)

                # Cursor Halo: paint a brief flash at the action point so the
                # user can follow Lucid's mouse activity at a glance. Only emit
                # when a coordinate is meaningful for this action.
                halo_coord = _halo_coord_for(action)
                if (
                    halo_coord is not None
                    and getattr(self.settings.overlay, "cursor_halo", True)
                ):
                    yield f"\n[halo] {action.action}|{halo_coord[0]},{halo_coord[1]}\n"

                time.sleep(self.settings.safety.pause_seconds)
                new_snapshot = ContextSnapshot.capture(self.settings)
                snapshot = new_snapshot

                # Captcha detection after every real action.
                if self._captcha is not None and self.settings.captcha.enabled:
                    detection = detect_captcha(new_snapshot.a11y_tree)
                    if detection is not None and action.action != "solve_captcha":
                        captcha_note = self._captcha.solve(detection)
                        annotated += f"\n[captcha] {captcha_note}"
                        captcha_hit = captcha_note

                observed = [self.provider.text_block(annotated)]
                if self._may_attach_image(new_snapshot):
                    observed.append(self.provider.image_block(new_snapshot.image))
                tool_results.append(
                    self.provider.tool_result_block(
                        action.id,
                        observed,
                    )
                )
                self._transcript.append(f"{action.action}: {annotated[:200]}")
                # Action log line — overlay filters the `[action-log]` prefix
                # out of the visible result pane and routes it to the docked
                # 10-entry debug panel. Safe no-op if the UI ignores it.
                yield (
                    "\n[action-log] "
                    f"{_fmt_action_for_log(action.action, action.params, result_text)}\n"
                )

                # Step Journal: persist before/after thumbnails + a one-line
                # record so the overlay's Step Gallery can rebuild the run
                # visually. The yield below carries enough metadata for the UI
                # to update without re-reading the JSONL file on every step.
                if self._journal is not None:
                    try:
                        record = self._journal.record(
                            action_name=action.action,
                            params=action.params,
                            before_image=journal_before_image,
                            after_image=new_snapshot.image,
                            outcome=result_text,
                            monitor_index=new_snapshot.monitor_index,
                        )
                        yield (
                            "\n[step] "
                            f"{self._journal.session_dir}|{record.id}|{record.action_name}|"
                            f"{record.after_thumb or record.before_thumb or ''}|"
                            f"{record.outcome_one_line()}\n"
                        )
                    except Exception as exc:
                        log.debug("journal record failed for %s: %s", action.action, exc)
                if captcha_hit:
                    # Let Claude see the update and decide on the next step.
                    break

            messages.append(Message(role="user", content=tool_results))

            if last_stop_reason == "end_turn" and not tool_uses:
                return

        if steps >= max_steps:
            yield f"\n[stopped] Reached max steps ({max_steps}).\n"
            self._transcript.append(f"[stopped] max steps {max_steps}")

    # ---------- helpers ----------

    def _initial_user_text(
        self, prompt: str, snapshot: ContextSnapshot, attachment_count: int = 0
    ) -> str:
        parts: list[str] = [f"Goal:\n{prompt}"]

        if attachment_count:
            parts.append(
                f"[{attachment_count} reference image(s) attached BEFORE this "
                "text, RIGHT AFTER the live desktop screenshot. Use them as "
                "visual specifications — the user wants the live desktop to "
                "end up looking like those references, or wants you to "
                "reproduce the action shown in them.]"
            )

        profile_block = ""
        if self.profile is not None:
            profile_block = pick_profile_snippet(self.profile, context_hint=prompt)
        if profile_block:
            parts.append(profile_block)

        context = snapshot.to_prompt_context()
        if context:
            parts.append(f"Screen context:\n{context}")

        a11y = _flatten_a11y(snapshot.a11y_tree)
        parts.append("Accessibility tree (named clickable elements with bounds):\n" + a11y)

        if self.store is not None and self.settings.memory.enabled:
            mem = memory_context_block(self.store, prompt, target_app=_snapshot_app(snapshot))
            if mem:
                parts.append(mem)

        return "\n\n".join(parts)

    def _final_proof(self, _stale_snapshot: ContextSnapshot) -> Iterator[str]:
        """Capture a fresh screenshot after the task ends and save it as proof.

        Lucid saves the file under ``data/proofs/`` with a timestamp so the
        user can verify what the desktop looked like the moment Lucid
        considered the task done. The path is also recorded in memory.
        """
        try:
            final = ContextSnapshot.capture(self.settings)
        except Exception as exc:
            yield f"[proof] could not capture final screenshot: {exc}\n"
            return

        try:
            proofs_dir = self.settings.data_dir / "proofs"
            proofs_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            safe_goal = _slug_for_proof(self._current_goal)
            path = proofs_dir / f"{stamp}-{safe_goal}.png"
            final.image.save(path, format="PNG", optimize=True)
        except Exception as exc:
            yield f"[proof] could not save final screenshot: {exc}\n"
            return

        yield f"\n[proof] final screenshot saved: {path}\n"
        try:
            if self.store is not None:
                self.store.touch_file(str(path), kind="proof", tags=self._current_goal[:80])
        except Exception as exc:
            log.debug("proof file memory index failed: %s", exc)

    def _run_action_with_timeout(self, action: ActionBlock, timeout: float) -> str:
        """Run a single action with a wall-clock timeout.

        ``pyautogui`` calls can hang (stuck modal dialog, wedged input queue).
        We run in a worker thread and abandon if it doesn't return in time.
        The result is best-effort: if the thread times out we still return a
        descriptive message so the loop continues instead of blocking.
        """
        if timeout <= 0:
            return self.actions.run(action)

        holder: dict[str, str] = {}

        def _worker() -> None:
            try:
                holder["result"] = self.actions.run(action)
            except Exception as exc:
                holder["result"] = f"error: {exc}"

        t = threading.Thread(target=_worker, daemon=True, name=f"lucid-action-{action.action}")
        t.start()
        t.join(timeout)
        if t.is_alive():
            log.warning("action %s exceeded per-action timeout %.1fs", action.action, timeout)
            return f"[timeout] action '{action.action}' exceeded {timeout:.0f}s"
        return holder.get("result", f"[unknown] action '{action.action}' produced no result")

    def _maybe_route_to_workflow(
        self,
        prompt: str,
        snapshot: ContextSnapshot,
        cancel: threading.Event,
    ) -> Iterator[str] | None:
        """Return a replay iterator when ``prompt`` matches a saved workflow.

        Heuristic: the first non-whitespace line of the prompt is queried
        against the workflow registry. If we get a hit, we extract the
        required variables (via ``_extract_variables``) and hand off to
        :class:`SemanticReplayer`. A ``None`` return lets the caller fall
        through to the regular Execute loop.
        """
        if (
            not prompt
            or prompt.strip().startswith("teach:")
            or prompt.strip().startswith("noworkflow:")
        ):
            return None
        first_line = prompt.strip().splitlines()[0]
        if len(first_line) < 4:
            return None

        registry = WorkflowRegistry(self.settings.workflows_dir)
        entry = registry.find(first_line)
        if entry is None:
            return None

        wf_path = self.settings.workflows_dir / entry.path
        if not wf_path.exists():
            return None
        workflow = load_workflow(wf_path)
        variables = self._extract_variables(prompt, entry, snapshot)

        missing = [v.name for v in workflow.variables if v.required and not variables.get(v.name)]

        def _replay() -> Iterator[str]:
            yield f"[workflow] matched {entry.slug} (via {first_line!r})\n"
            if variables:
                yield f"[workflow] variables: {variables}\n"
            if missing:
                yield (
                    f"[workflow] missing required variable(s): {', '.join(missing)}\n"
                    f"[workflow] tell me the value(s) in the next prompt and I'll continue.\n"
                )
                return
            replayer = SemanticReplayer(self.settings, self.provider)
            for chunk in replayer.run(workflow, cancel, variables=variables):
                yield chunk

        return _replay()

    def _extract_variables(
        self,
        prompt: str,
        entry: WorkflowEntry,
        snapshot: ContextSnapshot,
    ) -> dict[str, str]:
        """Use the LLM to pull each required variable value out of ``prompt``.

        Falls back to a regex-style heuristic when the LLM is unavailable
        or returns garbage, so the routing path still works offline for
        single-variable workflows with obvious quoting.
        """
        if not entry.variables:
            return {}

        var_descriptions = "\n".join(
            f"- {v.name}: {v.description or v.name}"
            + (f" (example: {v.example})" if v.example else "")
            for v in entry.variables
        )
        schema_keys = ", ".join(f'"{v.name}"' for v in entry.variables)
        user_text = (
            f"Workflow: {entry.slug} — {entry.name}\n"
            f"Required variables:\n{var_descriptions}\n\n"
            f"User prompt:\n{prompt}\n\n"
            f"Return ONLY a JSON object with keys [{schema_keys}]. Unknown values → empty string."
        )
        messages = [Message(role="user", content=[self.provider.text_block(user_text)])]

        buffer: list[str] = []
        try:
            for event in self.provider.stream(messages, system=None, max_tokens=300):
                if event.kind == "text_delta":
                    buffer.append(event.text)
                elif event.kind == "done" or event.kind == "error":
                    break
        except Exception as exc:
            log.debug("variable extraction stream failed: %s", exc)

        raw = "".join(buffer).strip()
        import json
        import re

        match = re.search(r"\{.*\}", raw, re.DOTALL)
        extracted: dict[str, str] = {}
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    extracted = {
                        str(k): str(v)
                        for k, v in parsed.items()
                        if isinstance(v, (str, int, float))
                    }
            except json.JSONDecodeError:
                pass

        # Regex fallback: look for ``var=value`` pairs explicitly typed in the prompt.
        for v in entry.variables:
            if extracted.get(v.name):
                continue
            m = re.search(
                rf"\b{re.escape(v.name)}\s*[:=]\s*['\"]?([^'\"\n]+?)['\"]?(?:\s|$)",
                prompt,
                re.IGNORECASE,
            )
            if m:
                extracted[v.name] = m.group(1).strip()

        return extracted

    def _system_prompt(self) -> str:
        """Return SYSTEM_PROMPT plus any feature-gated addenda (browser, MCP)."""
        extras: list[str] = []
        if getattr(self.settings, "browser", None) and self.settings.browser.enabled:
            extras.append(
                "WEB OTOMASYON. `browser_*` action family is preferred for any "
                "DOM-bearing web work: `browser_launch` → `browser_goto` → "
                "`browser_click_selector` / `browser_fill` / `browser_press` / "
                "`browser_wait_for` / `browser_screenshot` / `browser_close`. "
                "Keep CSS selectors durable -- prefer `[data-testid=...]`, `id`, "
                "`aria-label`. Fall back to pyautogui only when the target is "
                "the user's existing Chrome window."
            )
        mcp_names = self._registered_mcp_action_names()
        if mcp_names:
            preview = ", ".join(mcp_names[:12])
            more = "" if len(mcp_names) <= 12 else f" (+{len(mcp_names) - 12} more)"
            extras.append(
                "EXTERNAL TOOLS AVAILABLE via the MCP bridge: "
                f"{preview}{more}. Use these like any other action when the "
                "task fits (file system reads, web search, custom servers). "
                "Each MCP action accepts a JSON `arguments` object whose keys "
                "match the server's published input schema."
            )
        if not extras:
            return SYSTEM_PROMPT
        return SYSTEM_PROMPT + "\n\n" + "\n\n".join(extras)

    @staticmethod
    def _registered_mcp_action_names() -> list[str]:
        """Read currently-registered MCP action names from the registry."""
        try:
            from lucid.actions import registry as _registry

            return sorted(n for n in _registry.available() if n.startswith("mcp_"))
        except Exception:
            return []

    def _open_journal_if_enabled(self, goal: str) -> None:
        """Open a fresh Step Journal session unless the user disabled it."""
        cfg = getattr(self.settings, "journal", None)
        if cfg is None or not getattr(cfg, "enabled", True):
            self._journal = None
            return
        try:
            self._journal = StepJournal.open_session(
                self.settings.journals_dir,
                goal=goal,
                thumb_width=cfg.thumb_width,
                webp_quality=cfg.webp_quality,
                max_sessions=cfg.max_sessions,
            )
        except Exception as exc:
            log.warning("could not open step journal: %s", exc)
            self._journal = None

    def _record_if_enabled(self, budget: RetryBudget) -> None:
        if self._fact_recorder is None or not self._current_goal:
            return
        transcript = "\n".join(self._transcript[-40:])
        try:
            self._fact_recorder.record_task(
                goal=self._current_goal,
                target_app=self._target_app,
                transcript=transcript,
                step_count=len(self._transcript),
                succeeded=not self._cancel.is_set() and "[error]" not in transcript,
            )
        except Exception as exc:
            log.debug("fact recording skipped: %s", exc)


def _slug_for_proof(goal: str) -> str:
    """Tiny filename-safe slug for saved proof screenshots."""
    import re as _re

    cleaned = _re.sub(r"[^\w\-]+", "_", (goal or "task"))
    return cleaned.strip("_")[:40] or "task"


def _snapshot_app(snapshot: ContextSnapshot) -> str | None:
    if snapshot.active is None:
        return None
    return (snapshot.active.process or "").lower() or None


def _translate_coords(action: ActionBlock, snapshot: ContextSnapshot) -> ActionBlock:
    """Map image-space coordinates in an action back to absolute screen pixels."""
    new_params = dict(action.params)
    for key in ("coordinate", "start_coordinate", "end_coordinate"):
        val = new_params.get(key)
        if isinstance(val, (list, tuple)) and len(val) == 2:
            sx, sy = snapshot.image_to_screen(int(val[0]), int(val[1]))
            new_params[key] = [sx, sy]
    return ActionBlock(id=action.id, action=action.action, params=new_params)


def _flatten_a11y(tree: dict | None, limit: int = 60) -> str:
    """Flatten the accessibility tree into a short list Claude can consume."""
    if not tree:
        return "(no accessibility data available)"
    lines: list[str] = []

    def walk(node: dict, depth: int = 0) -> None:
        if len(lines) >= limit:
            return
        name = (node.get("name") or "").strip()
        role = (node.get("role") or "").strip()
        bounds = node.get("bounds")
        if name and bounds and len(bounds) == 4 and role not in {"Pane", "Window"}:
            cx = (bounds[0] + bounds[2]) // 2
            cy = (bounds[1] + bounds[3]) // 2
            lines.append(f"{role or '?'} | {name[:60]} | {cx},{cy}")
        for child in node.get("children", []) or []:
            if len(lines) >= limit:
                return
            walk(child, depth + 1)

    walk(tree)
    if not lines:
        return "(accessibility tree present but no named clickable elements)"
    return "\n".join(lines)


def _prune_old_images(messages: list, keep_last: int = 2) -> None:
    """Drop old screenshots from tool_result blocks to keep context light."""
    tool_result_msg_indices: list[int] = []
    for i, msg in enumerate(messages):
        content = msg.content
        if (
            msg.role == "user"
            and isinstance(content, list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        ):
            tool_result_msg_indices.append(i)
    if len(tool_result_msg_indices) <= keep_last:
        return
    preserve = set(tool_result_msg_indices[-keep_last:])
    for i in tool_result_msg_indices:
        if i in preserve:
            continue
        for block in messages[i].content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            inner = block.get("content")
            if not isinstance(inner, list):
                continue
            had_image = False
            new_inner: list[dict] = []
            for b in inner:
                if isinstance(b, dict) and b.get("type") == "image":
                    had_image = True
                    continue
                new_inner.append(b)
            if had_image:
                new_inner.append({"type": "text", "text": "[earlier screenshot omitted]"})
            block["content"] = new_inner


def _emit_thought(text: str) -> str:
    """Wrap a thought payload in the stream protocol prefix the overlay parses."""
    safe = text.replace("\n", " ").strip()
    return f"\n[thought] {safe}\n"


def _fmt_plan(action: ActionBlock) -> str:
    """One-line summary of an action the model is about to run (for ThoughtChain)."""
    params = action.params or {}
    bits: list[str] = []
    for key in ("element_name", "window_title", "file_path", "url", "command"):
        value = params.get(key)
        if value:
            bits.append(f"{key}={value!r}")
            break
    if not bits and params.get("keys"):
        bits.append("keys=" + "+".join(str(k) for k in params["keys"]))
    if not bits and params.get("text"):
        snippet = str(params["text"]).replace("\n", " ")
        if len(snippet) > 30:
            snippet = snippet[:29] + "…"
        bits.append(f"text={snippet!r}")
    if not bits and params.get("coordinate"):
        bits.append(f"@{tuple(params['coordinate'])}")
    detail = " ".join(bits) if bits else "(no params)"
    return f"{action.action}  {detail}"


def _halo_coord_for(action: ActionBlock) -> tuple[int, int] | None:
    """Return the screen coordinate the halo should flash at, if any."""
    params = action.params or {}
    for key in ("coordinate", "start_coordinate", "end_coordinate"):
        value = params.get(key)
        if isinstance(value, (list, tuple)) and len(value) == 2:
            try:
                return (int(value[0]), int(value[1]))
            except (TypeError, ValueError):
                continue
    return None


def _fmt_action_for_log(name: str, params: dict, result: str) -> str:
    """Compact one-liner for the overlay's action-log panel."""
    bits = [name]
    if params.get("element_name"):
        bits.append(f"'{params['element_name']}'")
    elif params.get("window_title"):
        bits.append(f"window={params['window_title']!r}")
    elif params.get("file_path"):
        bits.append(f"path={params['file_path']!r}")
    elif params.get("keys"):
        bits.append("+".join(params["keys"]))
    elif params.get("coordinate"):
        bits.append(f"@{tuple(params['coordinate'])}")
    elif params.get("text"):
        t = str(params["text"])[:30]
        bits.append(f"type={t!r}")
    elif params.get("command"):
        c = str(params["command"])[:40]
        bits.append(f"run={c!r}")
    outcome = (result or "").strip().splitlines()[0] if result else ""
    outcome = outcome[:60]
    return f"{' '.join(bits)}  →  {outcome}" if outcome else " ".join(bits)


def _merge_text(blocks: list[dict]) -> list[dict]:
    merged: list[dict] = []
    buffer = ""
    for b in blocks:
        if b.get("type") == "text":
            buffer += b.get("text", "")
        else:
            if buffer:
                merged.append({"type": "text", "text": buffer})
                buffer = ""
            merged.append(b)
    if buffer:
        merged.append({"type": "text", "text": buffer})
    return merged
