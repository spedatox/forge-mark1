"""Loop state and the single typed exit (§3).

All cross-iteration state lives in one mutable LoopState; the loop returns exactly
one Terminal with an enumerated reason. No loop state is scattered across
free-standing variables, and there is no second 'are we done' signal.

The loop is a state machine, and both of its edges are named. `StopReason` closes
the set of ways it can end; `ContinueReason` closes the set of ways it can go
round again. The second half matters as much as the first: a loop whose only
recovery vocabulary is 'it continued' cannot report what it recovered from, and
cannot be tested for having recovered at all. Every `continue` site in the engine
stamps a reason, and the stamps accumulate — so the journal, the post-mortem, and
the test all read the same names."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from forge.warden.ledger import TokenLedger


class StopReason(str, enum.Enum):
    """The complete, closed set of ways the loop can end (§3)."""
    COMPLETED = "completed"          # the model stopped requesting tools — the only success path
    ABORTED = "aborted"              # interrupt signal fired at a boundary
    MAX_ITERATIONS = "max_iterations"  # the single iteration ceiling was hit
    ERROR = "error"                  # an unrecoverable failure; surfaced loudly (§9.5)


class ContinueReason(str, enum.Enum):
    """The complete, closed set of ways the loop goes round again.

    Today there is one: the model asked for tools, they ran, take another turn.
    Recovery paths (retry a transient stream failure, compact and re-attempt)
    join this enum as they land, and each one is a distinct name rather than a
    silent extra lap."""
    NEXT_TURN = "next_turn"          # tools executed, results appended, continue
    RETRY_TRANSIENT = "retry_transient"   # the stream failed in a way that may not recur
    RECOVERED_CONTEXT = "recovered_context"   # the window was full; it was made smaller


@dataclass(frozen=True)
class Transition:
    """One trip around the loop, and why."""
    reason: ContinueReason
    detail: str = ""                 # free-form: which error, how many attempts


@dataclass
class LoopState:
    """The one mutable object threaded through the loop. Continue-sites append to
    `messages`, bump `iteration`, and stamp a transition; nothing else carries
    state between turns."""
    messages: list[dict[str, Any]]
    iteration: int = 0
    last_text: str = ""              # most recent assistant text, returned on COMPLETED
    transitions: list[Transition] = field(default_factory=list)
    ledger: TokenLedger = field(default_factory=TokenLedger)
    retries: int = 0
    """Total laps spent re-attempting rather than progressing, for the lifetime
    of the job. Subtracted from the iteration budget: a retry is not work done,
    and charging it against the ceiling lets a flaky provider quietly shorten
    every job it touches. Never reset — the budget it protects is cumulative."""

    compact_failures: int = 0
    """Consecutive failed attempts to reclaim context. Past a small limit Forge
    stops trying: a context that cannot be reduced will not become reducible on
    the fourth attempt, and an unbounded compaction retry is a money fire rather
    than a resilience feature."""

    retry_attempt: int = 0
    """*Consecutive* failed attempts at the current turn. Reset by any turn that
    streams cleanly, so the attempt limit bounds one bad patch rather than a
    job's lifetime total — six separate blips over an hour are six recoveries,
    not an exhausted budget."""

    @property
    def transition(self) -> Transition | None:
        """Why the previous iteration continued. None during the first."""
        return self.transitions[-1] if self.transitions else None

    def advance(self, reason: ContinueReason, detail: str = "") -> None:
        """Record why the loop is about to go round again. The engine calls this
        at every continue site — an unstamped lap is a bug, and the invariant is
        cheap to assert because `iteration` and `len(transitions)` move together."""
        self.transitions.append(Transition(reason, detail))


@dataclass(frozen=True)
class Terminal:
    """The sole return value of Warden.run."""
    reason: StopReason
    final_text: str = ""
    iterations: int = 0
    error: str | None = None         # real error text when reason == ERROR (fail loud)
    messages: list[dict[str, Any]] = field(default_factory=list)
    transitions: tuple[Transition, ...] = ()   # the path taken, in order
    usage: dict[str, Any] = field(default_factory=dict)   # the ledger's closing snapshot
