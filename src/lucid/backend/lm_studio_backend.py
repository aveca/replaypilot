"""LM Studio / OpenAI-compatible local LLM backend.

LM Studio exposes an OpenAI-compatible HTTP server (default
``http://localhost:1234/v1``) hosting any GGUF model the user downloaded
(Gemma, Llama, Qwen, etc.). Same endpoint shape as Ollama's OpenAI
adapter, so this backend works for both with a URL swap.

**Scope:** Answer + Teach streaming. Execute mode (tool_use streaming
against our custom ``computer`` tool) works if the chosen model supports
OpenAI function-calling (Gemma 2, Qwen 2.5, Llama 3.3, Mistral Large).
Smaller models may degrade — user's choice.

No API key, no metered billing, fully offline. Privacy-sensitive
workflows should prefer this backend.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from collections.abc import Iterator
from typing import Any

from PIL import Image

from lucid.llm.provider import LLMProvider, Message
from lucid.llm.schemas import ComputerUseBlock, StreamEvent

log = logging.getLogger("lucid.backend.lm_studio")


class LMStudioProvider(LLMProvider):
    """OpenAI-compatible streaming provider for local LM Studio servers."""

    name = "lm_studio"

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or "lm-studio"
        self._model = model
        self._client = None
        self._probed = False

    def _get_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "openai package required for lm_studio backend: uv pip install openai"
                ) from exc
            self._client = OpenAI(base_url=self._base_url, api_key=self._api_key)
        return self._client

    def _probe_and_resolve_model(self, requested: str | None) -> str:
        """Confirm the local server is reachable and pick a model name.

        - Empty model -> pick the first model the server reports.
        - Otherwise verify the requested model is in the listing; warn but
          do not block if the server's /models endpoint is unavailable.
        """
        target = (requested or self._model or "").strip()
        if self._probed and target:
            return target
        try:
            import httpx  # type: ignore

            url = f"{self._base_url}/models"
            response = httpx.get(url, timeout=4.0)
            response.raise_for_status()
            payload = response.json()
            ids = [
                item.get("id")
                for item in payload.get("data", [])
                if isinstance(item, dict) and item.get("id")
            ]
        except Exception as exc:  # noqa: BLE001
            log.debug("LM Studio /models probe skipped: %s", exc)
            ids = []

        if not target:
            if not ids:
                raise RuntimeError(
                    "LM Studio reported no loaded models. Open LM Studio, load a "
                    "model, then retry. (probed: " + self._base_url + "/models)"
                )
            target = ids[0]
            self._model = target
            log.info("LM Studio: auto-selected model %r", target)
        elif ids and target not in ids:
            log.warning(
                "LM Studio: model %r not in server's listing (%s); request will "
                "still be attempted but may 404",
                target,
                ", ".join(ids[:5]) + ("..." if len(ids) > 5 else ""),
            )
        self._probed = True
        return target

    def stream(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 2048,
        model: str | None = None,
        cache_system: bool = False,
        cache_tools: bool = False,
    ) -> Iterator[StreamEvent]:
        client = self._get_client()
        oai_messages: list[dict[str, Any]] = []
        if system:
            oai_messages.append({"role": "system", "content": system})
        for msg in messages:
            oai_messages.extend(_to_openai_message(msg))

        oai_tools: list[dict[str, Any]] | None = None
        if tools:
            oai_tools = [_to_openai_tool(t) for t in tools if t]

        try:
            resolved_model = self._probe_and_resolve_model(model)
        except RuntimeError as exc:
            yield StreamEvent(kind="error", error=str(exc))
            return

        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools
            kwargs["tool_choice"] = "auto"

        try:
            stream = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.exception("LM Studio request failed: %s", exc)
            yield StreamEvent(kind="error", error=str(exc))
            return

        # Accumulate tool-call chunks since OpenAI streams them piecewise.
        pending_tool_calls: dict[int, dict[str, Any]] = {}
        # The model sometimes answers with the action as JSON *in the content*
        # instead of using the native tool_calls channel. Collect the text so
        # the end-of-stream normaliser can recover it (see _normalise_text).
        text_parts: list[str] = []
        emitted_tool_use = False

        try:
            for chunk in stream:
                try:
                    choice = chunk.choices[0]
                except (AttributeError, IndexError):
                    continue
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                text_delta = getattr(delta, "content", None)
                if text_delta:
                    text_parts.append(text_delta)
                    yield StreamEvent(kind="text_delta", text=text_delta)

                tcs = getattr(delta, "tool_calls", None) or []
                for tc in tcs:
                    idx = getattr(tc, "index", 0) or 0
                    slot = pending_tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            slot["args"] += fn.arguments

                finish = getattr(choice, "finish_reason", None)
                if finish in ("tool_calls", "stop", "length"):
                    for slot in pending_tool_calls.values():
                        try:
                            args = json.loads(slot["args"] or "{}")
                        except json.JSONDecodeError:
                            args = {"_raw": slot["args"]}
                        coord = args.get("coordinate")
                        coord_tuple = (
                            (int(coord[0]), int(coord[1]))
                            if isinstance(coord, (list, tuple)) and len(coord) == 2
                            else None
                        )
                        # The action usually lives in the arguments, but some
                        # models put it in the function name instead, or omit
                        # it entirely. Recorded during validation: tool_calls
                        # like {"element_name": "Submit"} with no "action" key
                        # produced ComputerUseBlock(action=""), which the
                        # executor silently ignores. Fall back to the function
                        # name so the action is not lost.
                        action = str(args.get("action") or slot.get("name") or "")
                        block = ComputerUseBlock(
                            id=slot["id"] or f"lmstudio-{id(slot)}",
                            action=action,
                            coordinate=coord_tuple,
                            text=args.get("text"),
                            keys=args.get("keys"),
                            duration_ms=args.get("duration_ms"),
                            scroll_direction=args.get("scroll_direction"),
                            scroll_amount=args.get("scroll_amount"),
                            raw={"input": args},
                        )
                        yield StreamEvent(kind="tool_use", tool_use=block)
                        emitted_tool_use = True
                    pending_tool_calls.clear()
                    if finish == "stop" and not emitted_tool_use and text_parts:
                        # The model chose prose over the native tool channel.
                        # Recover a well-formed action from it rather than
                        # throwing the turn away, which is what used to happen:
                        # the executor saw "no action emitted" and nudged, and
                        # with a large accessibility tree llama3.1:8b did this
                        # on every turn until the nudge budget ran out.
                        recovered = _normalise_text_action("".join(text_parts))
                        if recovered is not None:
                            log.info(
                                "recovered a tool_use from text content "
                                "(native tool_calls absent)"
                            )
                            yield StreamEvent(kind="tool_use", tool_use=recovered)
                    yield StreamEvent(kind="done", stop_reason=finish)
        except Exception as exc:
            log.exception("LM Studio stream error: %s", exc)
            yield StreamEvent(kind="error", error=str(exc))

    # ---------- content block factories ----------
    def image_block(self, img: Image.Image) -> dict[str, Any]:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        }

    def text_block(self, text: str) -> dict[str, Any]:
        return {"type": "text", "text": text}

    def tool_result_block(
        self,
        tool_use_id: str,
        content: list[dict[str, Any]] | str,
        is_error: bool = False,
    ) -> dict[str, Any]:
        # OpenAI format: tool role message with tool_call_id
        if isinstance(content, list):
            text_parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            content_str = "\n".join(text_parts) or json.dumps(content)[:400]
        else:
            content_str = str(content)
        return {
            "role": "tool",
            "tool_call_id": tool_use_id,
            "content": content_str if not is_error else f"[error] {content_str}",
        }


def _to_openai_message(msg: Message) -> list[dict[str, Any]]:
    """Translate Lucid's Anthropic-shaped Message into OpenAI chat format.

    Returns a list because a single Anthropic user message containing
    tool_result blocks must expand into multiple OpenAI messages: one
    role="tool" message per tool_result (with matching tool_call_id),
    plus an optional role="user" message for any remaining text/image
    content. OpenAI rejects assistant messages with tool_calls unless
    every tool_call_id has a paired tool message.
    """
    if msg.role == "assistant":
        # Assistant messages may contain text + tool_use blocks.
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in msg.content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", "computer"),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    }
                )
        out: dict[str, Any] = {
            "role": "assistant",
            "content": "\n".join(text_parts) if text_parts else None,
        }
        if tool_calls:
            out["tool_calls"] = tool_calls
        return [out]

    if msg.role == "user":
        tool_messages: list[dict[str, Any]] = []
        content_parts: list[Any] = []
        for block in msg.content:
            if not isinstance(block, dict):
                continue
            # Provider's tool_result_block already returns OpenAI shape
            # ({"role": "tool", "tool_call_id": ..., "content": ...}).
            # Forward it as a standalone message.
            if block.get("role") == "tool":
                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_call_id", ""),
                        "content": block.get("content", "") or "[no output]",
                    }
                )
                continue
            btype = block.get("type")
            if btype == "text":
                content_parts.append({"type": "text", "text": block.get("text", "")})
            elif btype == "image_url":
                # Already in OpenAI shape, which is what this provider's own
                # image_block() returns. Forward it untouched.
                #
                # Before this branch existed, image_block() produced
                # {"type": "image_url", ...} while the loop below only matched
                # the Anthropic shape {"type": "image", "source": {...}}. The
                # block matched neither branch and was dropped without a
                # warning, so every screenshot silently vanished from the
                # request and vision models were asked to answer blind. A
                # vision-capable model then answers "I cannot see" while the
                # same request built by hand works fine.
                content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": block.get("image_url", {}),
                    }
                )
            elif btype == "image":
                src = block.get("source", {})
                data = src.get("data", "")
                mime = src.get("media_type", "image/png")
                content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{data}"},
                    }
                )
            elif btype == "tool_result":
                inner = block.get("content", "")
                if isinstance(inner, list):
                    txt = "\n".join(
                        b.get("text", "")
                        for b in inner
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    txt = str(inner)
                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": txt or "[no output]",
                    }
                )

        result: list[dict[str, Any]] = list(tool_messages)
        if content_parts:
            if len(content_parts) == 1 and content_parts[0].get("type") == "text":
                result.append({"role": "user", "content": content_parts[0]["text"]})
            else:
                result.append({"role": "user", "content": content_parts})
        return result

    # Fallback (system etc.) — pass through as string
    if msg.content and isinstance(msg.content[0], dict):
        return [{"role": msg.role, "content": msg.content[0].get("text", "")}]
    return [{"role": msg.role, "content": ""}]


def _to_openai_tool(anthropic_tool: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic tool definition into OpenAI function-call format."""
    return {
        "type": "function",
        "function": {
            "name": anthropic_tool.get("name", "computer"),
            "description": anthropic_tool.get("description", ""),
            "parameters": anthropic_tool.get("input_schema", {"type": "object"}),
        },
    }

# Accepted aliases for the action name when a model writes the action in prose
# instead of using the native tool_calls channel.
_NAME_KEYS = ("name", "action")


def _extract_json_object(text: str) -> "dict[str, Any] | None":
    """First JSON object found in ``text``, or None.

    Tolerates prose and markdown fences around the object, which is common
    once a large context nudges a model off the tool channel.
    """
    candidate = (text or "").strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _parse_key_list(raw: str) -> list[str]:
    """Parse a keys value the model may have written several ways.

    Observed shapes: "['ctrl','l']" (Python literal), '["ctrl","l"]' (JSON),
    "ctrl+a" (joined). Returns [] for anything unparseable.
    """
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        # json.loads rejects the single-quoted Python literal the model emits,
        # so normalise quotes first. Measured: "['ctrl','l']".
        candidate = text.replace("'", '"')
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            return []
        if isinstance(decoded, list):
            return [str(k).strip() for k in decoded if str(k).strip()]
        return []
    if " " in text:
        # A real key name has no space; "hello world" is prose, not a chord.
        return []
    return [k.strip() for k in text.split("+") if k.strip()]


def _normalise_text_action(text: str) -> "ComputerUseBlock | None":
    """Turn a JSON action the model wrote in prose into a tool_use block.

    Accepts ``{"name": "click_element", "parameters": {...}}`` and the flatter
    ``{"action": "click_element", "element_name": "..."}``. Returns None for
    anything that is not unmistakably one action, so ordinary prose, lists and
    partial JSON never become a desktop action.

    Only called when the model emitted no native tool_calls, so this cannot
    shadow a well-formed tool call.
    """
    parsed = _extract_json_object(text)
    if not parsed:
        return None

    name = ""
    for key in _NAME_KEYS:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    if not name:
        return None

    inner = parsed.get("parameters")
    if isinstance(inner, dict):
        params: dict[str, Any] = dict(inner)
    else:
        params = {k: v for k, v in parsed.items() if k not in _NAME_KEYS}

    # The action is already named at the top level; a second action key inside
    # the parameters would be contradictory.
    params.pop("action", None)

    # Models frequently stringify the coordinate pair: "[462, 277]".
    coord = params.get("coordinate")
    if isinstance(coord, str):
        try:
            decoded = json.loads(coord)
            if isinstance(decoded, list) and len(decoded) == 2:
                params["coordinate"] = decoded
            else:
                params.pop("coordinate", None)
        except json.JSONDecodeError:
            params.pop("coordinate", None)
    coord_tuple = None
    if isinstance(params.get("coordinate"), (list, tuple)) and len(params["coordinate"]) == 2:
        coord_tuple = (int(params["coordinate"][0]), int(params["coordinate"][1]))

    keys = params.get("keys")
    if isinstance(keys, str):
        cleaned = _parse_key_list(keys)
        if cleaned:
            params["keys"] = cleaned
        else:
            params.pop("keys", None)

    return ComputerUseBlock(
        id="lmstudio-text-recovery",
        action=name,
        coordinate=coord_tuple,
        text=params.get("text"),
        keys=params.get("keys"),
        duration_ms=params.get("duration_ms"),
        scroll_direction=params.get("scroll_direction"),
        scroll_amount=params.get("scroll_amount"),
        raw={"input": params, "_recovered_from_text": True},
    )
