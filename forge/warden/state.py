"""Loop state and the single typed exit (§3).

All cross-iteration state lives in one mutable LoopState; the loop returns exactly
one Terminal with an enumerated reason. No loop state is scattered across
free-standing variables, and there is no second 'are we done' signal."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class StopReason(str, enum.Enum):
    """The complete, closed set of ways the loop can end (§3)."""
    COMPLETED = "completed"          # the model stopped requesting tools — the only success path
    ABORTED = "aborted"              # interrupt signal fired at a boundary
    MAX_ITERATIONS = "max_iterations"  # the single iteration ceiling was hit
    ERROR = "error"                  # an unrecoverable failure; surfaced loudly (§9.5)


@dataclass
class LoopState:
    """The one mutable object threaded through the loop. Continue-sites append to
    `messages` and bump `iteration`; nothing else carries state between turns."""
    messages: list[dict[str, Any]]
    iteration: int = 0
    last_text: str = ""              # most recent assistant text, returned on COMPLETED


@dataclass(frozen=True)
class Terminal:
    """The sole return value of Warden.run."""
    reason: StopReason
    final_text: str = ""
    iterations: int = 0
    error: str | None = None         # real error text when reason == ERROR (fail loud)
    messages: list[dict[str, Any]] = field(default_factory=list)
