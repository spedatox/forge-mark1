"""AnthropicModel — the real Warden reasoning client.

Streams text deltas during a turn and surfaces tool-use blocks once the turn
resolves. Honors the interrupt signal by stopping the stream promptly. There is
deliberately no model-fallback ladder and no token-escalation recovery (§3
rejected list): one model, fail loud.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from forge.model.base import TextDelta, ToolUseRequest


class AnthropicModel:
    def __init__(self, model_id: str, api_key: str, max_tokens: int = 4096) -> None:
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required for AnthropicModel. Set it, or run the "
                "demo which uses the ScriptedModel and needs no key.")
        # Imported lazily so the package imports without the SDK present.
        from anthropic import AsyncAnthropic
        self._client = AsyncAnthropic(api_key=api_key)
        self.model_id = model_id
        self.max_tokens = max_tokens

    async def stream(self, *, system: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]], signal: asyncio.Event
                     ) -> AsyncIterator[TextDelta | ToolUseRequest]:
        async with self._client.messages.stream(
            model=self.model_id,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        ) as stream:
            async for event in stream:
                if signal.is_set():
                    break
                if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                    yield TextDelta(event.delta.text)
            if signal.is_set():
                return
            final = await stream.get_final_message()
            for block in final.content:
                if getattr(block, "type", "") == "tool_use":
                    yield ToolUseRequest(id=block.id, name=block.name, input=dict(block.input))
