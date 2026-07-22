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


@dataclass
class UsageReport:
    """What one turn cost, yielded once after content.

    **Optional by contract.** A model that never yields this still works — the
    ledger falls back to a character estimate. That tolerance is what keeps a
    third-party provider cheap to add: reporting usage is a capability, not an
    obligation. `estimated` marks figures that were guessed rather than
    reported, so nothing downstream renders a guess as a measurement."""
    input_tokens: int
    output_tokens: int
    cache_read: int = 0
    cache_write: int = 0
    estimated: bool = False


ModelEvent = TextDelta | ToolUseRequest | UsageReport


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
    ) -> AsyncIterator[ModelEvent]:
        """Stream one assistant turn. Yields TextDelta and ToolUseRequest events,
        optionally a closing UsageReport; the generator ending marks turn
        completion. Must honor `signal` by stopping promptly when it is set."""
        ...
