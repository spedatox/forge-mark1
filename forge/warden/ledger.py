"""The token ledger — how full is the window, and what has this cost (H2).

`max_iterations` was doing double duty as a context guard, which it is not: a job
can die of a full window in six iterations or survive sixty. The ledger is the
real gauge, and the thresholds below are what compaction triggers on.

**Budgets are absolute, not percentages.** The obvious design is "compact at 80 %
full". The reference harness does something more careful, and the reason matters:

    effective = context_limit − reserve_for_the_summary_call
    compact_at = effective − headroom_for_one_more_turn
    blocking   = effective − headroom_for_a_manual_rescue

The reserve exists because the compaction call *itself* needs somewhere to put
its output. A percentage threshold silently reserves less on a smaller window —
so a local model with a 16 K context would trigger compaction at 12.8 K and then
have no room left to produce the summary, failing exactly when it is needed. An
absolute reserve does not have that failure mode. The emergent percentage on a
200 K window is ~85 %; that number is an output of the model's size, not an
input.

The ledger is a gauge, not a bill. Providers that report usage give exact
figures; those that do not get a `chars/4` estimate and are flagged `estimated`
so nothing downstream presents a guess as a measurement."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from forge.model.base import UsageReport

DEFAULT_CONTEXT_LIMIT = 200_000

# Room for the compaction call's own output. Without it, compaction triggers at a
# point where it cannot complete.
SUMMARY_RESERVE_TOKENS = 20_000
# Headroom below the effective limit: enough for one more full turn before the
# window is genuinely gone.
COMPACT_BUFFER_TOKENS = 13_000
# A last sliver kept back so an operator-driven rescue still has room to run.
BLOCKING_BUFFER_TOKENS = 3_000


@dataclass
class TokenLedger:
    """Per-job token accounting, owned by `LoopState`."""

    context_limit: int = DEFAULT_CONTEXT_LIMIT
    max_output_tokens: int = 16_384

    prompt_tokens: int = 0         # LAST turn's FULL prompt size — the fullness signal
    input_tokens: int = 0          # cumulative uncached input (what you pay full rate for)
    output_tokens: int = 0         # cumulative
    cache_read_tokens: int = 0     # cumulative
    cache_write_tokens: int = 0    # cumulative
    turns: int = 0
    estimated: bool = False        # true once any turn's figures were guessed

    # ── Thresholds ───────────────────────────────────────────────────────────
    # Each buffer is additionally clamped to a fraction of what it is carved out
    # of, so a small-context model gets proportionate headroom instead of a
    # negative budget. On a 200 K window the clamps never bind.
    @property
    def effective_limit(self) -> int:
        reserve = min(self.max_output_tokens, SUMMARY_RESERVE_TOKENS,
                      self.context_limit // 4)
        return self.context_limit - reserve

    @property
    def compact_at(self) -> int:
        effective = self.effective_limit
        return effective - min(COMPACT_BUFFER_TOKENS, effective // 8)

    @property
    def blocking_limit(self) -> int:
        effective = self.effective_limit
        return effective - min(BLOCKING_BUFFER_TOKENS, effective // 32)

    @property
    def fullness(self) -> float:
        """0.0–1.0 against the effective limit. For display; decisions use the
        absolute thresholds, which is the whole point of this module."""
        return min(1.0, self.prompt_tokens / max(1, self.effective_limit))

    def should_compact(self) -> bool:
        return self.prompt_tokens >= self.compact_at

    def is_blocked(self) -> bool:
        """Past the point where even a compaction call would not fit."""
        return self.prompt_tokens >= self.blocking_limit

    # ── Recording ────────────────────────────────────────────────────────────
    def record(self, report: UsageReport) -> None:
        """Fold in one turn's reported usage.

        `prompt_tokens` is **replaced**, not accumulated — it is the size of the
        prompt as last sent, which is what fullness means — and it is the SUM of
        uncached input plus both cache figures. Anthropic reports `input_tokens`
        net of the cache, so once prompt caching is on, a 150 K conversation
        arrives as a few thousand uncached tokens plus a large `cache_read`.
        Reading fullness off `input_tokens` alone would report an almost-empty
        window right up until the provider rejected the request.

        Everything else is a running total, because those are what a job cost."""
        self.prompt_tokens = report.input_tokens + report.cache_read + report.cache_write
        self.input_tokens += report.input_tokens
        self.output_tokens += report.output_tokens
        self.cache_read_tokens += report.cache_read
        self.cache_write_tokens += report.cache_write
        self.turns += 1
        if report.estimated:
            self.estimated = True

    def estimate(self, system: str, messages: list[dict[str, Any]]) -> None:
        """Fallback for a provider that reports nothing. Four characters per
        token is crude, and it is flagged as such — but a crude gauge beats
        flying blind into a provider error."""
        try:
            size = len(system) + len(json.dumps(messages, default=str))
        except (TypeError, ValueError):
            size = len(system) + sum(len(str(m)) for m in messages)
        self.record(UsageReport(input_tokens=size // 4, output_tokens=0, estimated=True))

    def snapshot(self) -> dict[str, Any]:
        """The shape that goes on a `usage` event and into the Terminal."""
        return {
            "prompt": self.prompt_tokens,
            "input": self.input_tokens,
            "output": self.output_tokens,
            "cache_read": self.cache_read_tokens,
            "cache_write": self.cache_write_tokens,
            "turns": self.turns,
            "context_limit": self.context_limit,
            "effective_limit": self.effective_limit,
            "fullness": round(self.fullness, 4),
            "estimated": self.estimated,
        }
