"""Seam 4 — event fan-out.

`emit` was already an injected callable, which made it a seam in fact. This makes
it one in name, and adds the property that matters once there is more than one
consumer: **a sink cannot fail the job.**

A journal that cannot write its file, a metrics exporter whose endpoint is down,
a dashboard that disconnected — none of those are reasons to stop executing. The
transport sink is not special-cased either: if the socket to Mark VI breaks, the
job carries on and finishes, and the peer's own reconnect logic deals with the
socket. Observation never governs execution.

Order is preserved and sinks are sequential. A journal that records events out of
order is worse than no journal, and the volume here is a handful of events per
turn.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from forge.gate.protocol import JobEvent

logger = logging.getLogger("forge.gate")

Sink = Callable[[JobEvent], Awaitable[None]]


class EventFan:
    """An ordered fan-out of JobEvents to independent sinks."""

    def __init__(self, sinks: list[Sink] | None = None) -> None:
        self._sinks: list[Sink] = list(sinks or [])

    def add(self, sink: Sink) -> None:
        """Append a sink. The registration point a Mark III subscriber calls."""
        self._sinks.append(sink)

    async def __call__(self, event: JobEvent) -> None:
        for sink in self._sinks:
            try:
                await sink(event)
            except Exception as e:  # noqa: BLE001 — see the module docstring
                logger.warning("event_sink_failed",
                               extra={"sink": getattr(sink, "__name__", repr(sink)),
                                      "event": event.type, "error": repr(e)})
