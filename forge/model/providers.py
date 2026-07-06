"""Multi-provider model routing, mirroring S.P.E.D.A. Mark VI's llm_client.

Routing is driven by the model ref: ``"provider:model"`` — e.g.
``openai:gpt-5.1``, ``gemini:gemini-2.5-flash``, ``zai:glm-4.6``,
``deepseek:deepseek-v4-pro``, ``ollama:llama3.1:8b``. A bare name routes to
Anthropic, so every existing profile keeps working.

The Warden loop speaks Anthropic content-block format throughout (its message
history is text / tool_use / tool_result blocks). Anthropic calls pass straight
through the native SDK (`AnthropicModel`). Every other provider shares ONE adapter
(`OpenAICompatModel`) built on OpenAI's client — OpenAI, Gemini's OpenAI-compat
endpoint, z.ai's paas/v4, DeepSeek's endpoint, and Ollama's /v1 all speak the same
chat-completions dialect. Translation to and from that dialect happens only at the
request boundary; the loop and tools never change.

This mirrors Mark VI's engine so an operator sees consistent behavior whether a
turn runs in-process on Mark VI or out here on the Forge.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator

from forge.model.base import TextDelta, ToolUseRequest

logger = logging.getLogger("forge.model")

# Provider → AsyncOpenAI constructor kwargs (given the settings object). Lazy so
# a missing key only matters when that provider is actually selected.
_OPENAI_COMPAT = {
    "openai": lambda s: {"api_key": s.openai_api_key},
    "gemini": lambda s: {"api_key": s.gemini_api_key,
                         "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/"},
    "zai": lambda s: {"api_key": s.zai_api_key,
                      "base_url": "https://api.z.ai/api/paas/v4/"},
    "deepseek": lambda s: {"api_key": s.deepseek_api_key,
                           "base_url": "https://api.deepseek.com"},
    "ollama": lambda s: {"api_key": "ollama", "base_url": s.ollama_base_url},
}
_PROVIDERS = {"anthropic", *_OPENAI_COMPAT}

# chat-completions finish_reason → the reason our loop cares about. We only need
# to know whether tools were requested; the loop's real stop signal is whether any
# ToolUseRequest was yielded, so this is informational.
_FINISH_TO_STOP = {"stop": "end_turn", "tool_calls": "tool_use",
                   "function_call": "tool_use", "length": "max_tokens"}


def parse_model_ref(ref: str) -> tuple[str, str]:
    """Split ``provider:model`` → (provider, model). Bare names are Anthropic.
    Only the first segment is matched, so Ollama tags like ``llama3.1:8b`` survive
    inside ``ollama:llama3.1:8b``."""
    provider, sep, rest = ref.partition(":")
    if sep and provider in _PROVIDERS:
        return provider, rest
    return "anthropic", ref


class OpenAICompatModel:
    """One adapter for every OpenAI-compatible provider. Implements the Warden's
    `Model` protocol: streams TextDelta during the turn, then yields a
    ToolUseRequest per accumulated tool call."""

    def __init__(self, provider: str, model_id: str, settings: Any, max_tokens: int = 4096) -> None:
        if provider not in _OPENAI_COMPAT:
            raise ValueError(f"not an OpenAI-compatible provider: {provider!r}")
        kwargs = _OPENAI_COMPAT[provider](settings)
        if provider != "ollama" and not kwargs.get("api_key"):
            raise RuntimeError(
                f"{provider} model requested but its API key is not set "
                f"(expected {provider.upper()}_API_KEY).")
        from openai import AsyncOpenAI
        self._client = AsyncOpenAI(**kwargs)
        self.provider = provider
        self.model_id = model_id
        self.max_tokens = max_tokens

    async def stream(self, *, system: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]], signal: asyncio.Event
                     ) -> AsyncIterator[TextDelta | ToolUseRequest]:
        params = _to_openai_params(self.provider, self.model_id, system, messages,
                                   tools, self.max_tokens)
        raw = await self._client.chat.completions.create(**params, stream=True)
        acc: dict[int, dict[str, str]] = {}
        try:
            async for chunk in raw:
                if signal.is_set():
                    break
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                if delta.content:
                    yield TextDelta(delta.content)
                for tc in delta.tool_calls or []:
                    slot = acc.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
        finally:
            await raw.close()
        if signal.is_set():
            return
        for idx in sorted(acc):
            slot = acc[idx]
            yield ToolUseRequest(id=slot["id"] or _gen_tool_id(),
                                 name=slot["name"],
                                 input=_parse_tool_args(slot["arguments"], slot["name"]))


# ── Anthropic content-block → chat-completions translation ───────────────────

def _to_openai_params(provider: str, model: str, system: str,
                      messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                      max_tokens: int) -> dict[str, Any]:
    msgs: list[dict[str, Any]] = []
    if system:
        msgs.append({"role": "system", "content": system})
    for m in messages:
        msgs.extend(_translate_message(m))

    params: dict[str, Any] = {"model": model, "messages": msgs}

    if tools:
        params["tools"] = [{
            "type": "function",
            "function": {"name": t["name"], "description": t.get("description", ""),
                         "parameters": t.get("input_schema", {"type": "object"})},
        } for t in tools]

    if max_tokens:
        # OpenAI deprecated max_tokens for reasoning models; the others still take it.
        params["max_completion_tokens" if provider == "openai" else "max_tokens"] = max_tokens

    # DeepSeek-V4 defaults thinking ON, which is incompatible with the tool loop
    # (it makes reasoning_content a required round-trip field once tool_use enters
    # history). Force non-thinking whenever tools are present — i.e. the whole
    # agent loop. Mirrors Mark VI's llm_client.
    if provider == "deepseek" and tools:
        params["extra_body"] = {"thinking": {"type": "disabled"}}
    return params


def _translate_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """One Anthropic-format message → one or more chat-completions messages.
    tool_result blocks become role:"tool" messages emitted before the user text,
    preserving their adjacency to the assistant tool_calls that produced them."""
    role = message["role"]
    content = message.get("content")
    if isinstance(content, str):
        return [{"role": role, "content": content}]

    tool_msgs: list[dict[str, Any]] = []
    user_parts: list[str] = []
    assistant_text: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for block in content or []:
        btype = block.get("type")
        if btype == "text":
            (assistant_text if role == "assistant" else user_parts).append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append({
                "id": block["id"], "type": "function",
                "function": {"name": block["name"],
                             "arguments": json.dumps(block.get("input") or {})},
            })
        elif btype == "tool_result":
            rc = block.get("content", "")
            if isinstance(rc, list):
                rc = "\n".join(p.get("text", "") for p in rc if isinstance(p, dict))
            tool_msgs.append({"role": "tool", "tool_call_id": block.get("tool_use_id", ""),
                              "content": rc if isinstance(rc, str) else str(rc)})

    out: list[dict[str, Any]] = []
    if role == "assistant":
        text = "\n".join(t for t in assistant_text if t)
        if tool_calls:
            # `content` must be "" not null here — z.ai GLM rejects null content
            # (error 1214) mid-loop; "" is valid for every OpenAI-compat provider.
            out.append({"role": "assistant", "content": text, "tool_calls": tool_calls})
        elif text:
            out.append({"role": "assistant", "content": text})
    else:
        out.extend(tool_msgs)
        if user_parts:
            out.append({"role": role, "content": "\n\n".join(user_parts)})
    return out


def _parse_tool_args(raw: str | None, tool_name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, ValueError):
        logger.warning("tool_args_unparseable", extra={"tool": tool_name})
        return {}


def _gen_tool_id() -> str:
    # Some compat layers (Gemini) omit tool-call ids; the loop needs one to pair
    # tool_use with tool_result. Generated, never hardcoded.
    return f"call_{uuid.uuid4().hex[:24]}"
