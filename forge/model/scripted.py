"""ScriptedModel — a deterministic Model for the demo and tests.

It proves the loop, the Cell, the Graphify sidecar, and streaming end-to-end with
no API key and no network. A 'script' is a list of step functions; each receives
the running message history (so a step can branch on a prior tool result — a real
act→observe→adapt, not a fixed tape) and returns the turn's text plus any
tool-use requests. When a step returns no tool calls, the loop's sole stop
condition fires and the run completes (§3)."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, AsyncIterator, Callable

from forge.model.base import TextDelta, ToolUseRequest

Step = Callable[[list[dict[str, Any]]], "tuple[str, list[ToolUseRequest]]"]


def tool_call(name: str, **kwargs: Any) -> ToolUseRequest:
    return ToolUseRequest(id=f"toolu_{uuid.uuid4().hex[:12]}", name=name, input=kwargs)


class ScriptedModel:
    model_id = "scripted-forge"

    def __init__(self, steps: list[Step]) -> None:
        self._steps = steps
        self._i = 0

    async def stream(self, *, system: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]], signal: asyncio.Event
                     ) -> AsyncIterator[TextDelta | ToolUseRequest]:
        if signal.is_set():
            return
        if self._i >= len(self._steps):
            # Nothing scripted remains → emit a closing line with no tools, which
            # is the loop's completion signal.
            for chunk in _stream_text("The task is complete."):
                yield TextDelta(chunk)
            return

        step = self._steps[self._i]
        self._i += 1
        text, calls = step(messages)

        for chunk in _stream_text(text):
            yield TextDelta(chunk)
            await asyncio.sleep(0)      # give the event loop a beat so streaming is observable
        for call in calls:
            yield call


def _stream_text(text: str, width: int = 24) -> list[str]:
    if not text:
        return []
    return [text[i:i + width] for i in range(0, len(text), width)]
