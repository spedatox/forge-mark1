"""The Model protocol and its streamed event types.

Kept intentionally tiny: a model streams text deltas and tool-use requests for
one turn, then the generator ends. The engine turns that into an assistant
message and decides — solely from whether any tool-use requests arrived —
whether to loop again (§3)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolUseRequest:
    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Model(Protocol):
    model_id: str

    def stream(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        signal: asyncio.Event,
    ) -> AsyncIterator[TextDelta | ToolUseRequest]:
        """Stream one assistant turn. Yields TextDelta and ToolUseRequest events;
        the generator ending marks turn completion. Must honor `signal` by
        stopping promptly when it is set."""
        ...
