"""The token ledger (H2): budgets, the cache trap, and the estimate fallback."""
import asyncio
from typing import Any, AsyncIterator

import pytest
from pydantic import BaseModel

from forge.model.base import TextDelta, UsageReport
from forge.model.scripted import ScriptedModel, tool_call
from forge.warden.engine import Warden
from forge.warden.filestate import FileStateCache
from forge.warden.ledger import DEFAULT_CONTEXT_LIMIT, TokenLedger
from forge.warden.permissions import PermissionEngine
from forge.warden.tool import Tool, ToolContext, ToolResult


class NudgeArgs(BaseModel):
    pass


class Nudge(Tool):
    name = "nudge"
    description = "do nothing"
    Args = NudgeArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True

    async def call(self, args: NudgeArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult("ok")


class Reporting:
    """A model that reports usage, the way Anthropic does."""
    model_id = "reporting"

    def __init__(self, reports: list[UsageReport]) -> None:
        self.reports = reports
        self.i = 0

    async def stream(self, *, system, messages, tools, signal) -> AsyncIterator[Any]:
        yield TextDelta("done")
        if self.i < len(self.reports):
            yield self.reports[self.i]
            self.i += 1


class Silent:
    """A provider that reports nothing — the ledger must estimate."""
    model_id = "silent"

    async def stream(self, *, system, messages, tools, signal) -> AsyncIterator[Any]:
        yield TextDelta("done")


def _warden(model, **kwargs) -> Warden:
    return Warden(system_prompt="sys", tools={"nudge": Nudge()}, model=model,
                  ctx=ToolContext(agent_id="t", cell=None, graph=None,
                                  files=FileStateCache(), permissions=PermissionEngine(),
                                  network_allowed=False),
                  **kwargs)


# ── Budgets are absolute ─────────────────────────────────────────────────────
def test_the_reserve_comes_off_the_top():
    """Effective limit = window minus room for the compaction call's output."""
    led = TokenLedger(context_limit=200_000, max_output_tokens=16_384)
    assert led.effective_limit == 200_000 - 16_384
    assert led.compact_at == led.effective_limit - 13_000
    assert led.blocking_limit == led.effective_limit - 3_000
    assert led.compact_at < led.blocking_limit < led.effective_limit


def test_the_reserve_is_capped_at_twenty_thousand():
    """A model willing to emit 64K output does not get 64K reserved."""
    led = TokenLedger(context_limit=200_000, max_output_tokens=64_000)
    assert led.effective_limit == 200_000 - 20_000


def test_a_small_window_gets_proportionate_headroom_not_a_negative_budget():
    """The failure this guards: fixed buffers subtracted from a 16K window give
    a threshold below zero, and every turn would 'need compaction'."""
    led = TokenLedger(context_limit=16_000, max_output_tokens=16_384)
    assert 0 < led.compact_at < led.blocking_limit < led.effective_limit < 16_000
    led.prompt_tokens = 1
    assert not led.should_compact()


def test_thresholds_fire_in_order():
    led = TokenLedger()
    led.prompt_tokens = led.compact_at - 1
    assert not led.should_compact() and not led.is_blocked()
    led.prompt_tokens = led.compact_at
    assert led.should_compact() and not led.is_blocked()
    led.prompt_tokens = led.blocking_limit
    assert led.should_compact() and led.is_blocked()


# ── The cache trap ───────────────────────────────────────────────────────────
def test_fullness_counts_cached_tokens():
    """Anthropic reports input_tokens NET of the cache. A 150K conversation
    arrives as a few thousand uncached tokens plus a large cache_read — reading
    fullness off input_tokens alone would report an almost-empty window right up
    until the provider rejected the request."""
    led = TokenLedger(context_limit=200_000, max_output_tokens=16_384)
    led.record(UsageReport(input_tokens=2_000, output_tokens=500, cache_read=175_000))

    assert led.prompt_tokens == 177_000
    assert led.should_compact()
    # The bug this exists to prevent: 2_000 uncached tokens is nowhere near any
    # threshold, so a ledger reading input_tokens alone would sail past the
    # window and only find out when the provider refused the request.
    assert 2_000 < led.compact_at


def test_prompt_size_is_replaced_while_costs_accumulate():
    led = TokenLedger()
    led.record(UsageReport(input_tokens=1_000, output_tokens=100))
    led.record(UsageReport(input_tokens=1_500, output_tokens=200))
    assert led.prompt_tokens == 1_500      # the window as last sent
    assert led.input_tokens == 2_500       # what the job cost
    assert led.output_tokens == 300
    assert led.turns == 2


# ── The estimate fallback ────────────────────────────────────────────────────
def test_a_silent_provider_is_estimated_and_flagged():
    term = asyncio.run(_warden(Silent()).run("go"))
    assert term.usage["prompt"] > 0
    assert term.usage["estimated"] is True


def test_a_reporting_provider_is_not_flagged():
    model = Reporting([UsageReport(input_tokens=1_234, output_tokens=56)])
    term = asyncio.run(_warden(model).run("go"))
    assert term.usage["prompt"] == 1_234
    assert term.usage["output"] == 56
    assert term.usage["estimated"] is False


def test_usage_is_emitted_every_turn():
    events: list[dict] = []

    async def sink(ev):
        events.append(ev)

    steps = [lambda m: ("x", [tool_call("nudge")]), lambda m: ("done", [])]
    asyncio.run(_warden(ScriptedModel(steps), emit=sink).run("go"))
    usage = [e for e in events if e["type"] == "usage"]
    assert len(usage) == 2
    assert usage[0]["data"]["iteration"] == 1 and usage[1]["data"]["iteration"] == 2


def test_the_estimate_measures_the_prompt_as_sent_not_the_reply():
    """Accounting happens before the assistant turn joins the transcript, so a
    long reply does not retroactively inflate the window it was produced from."""
    short = asyncio.run(_warden(Silent()).run("go"))
    long = asyncio.run(_warden(Silent()).run("go " * 500))
    assert long.usage["prompt"] > short.usage["prompt"]


def test_the_ledger_survives_unserializable_messages():
    """The estimate JSON-encodes the transcript; a stray object must degrade to
    a rough size rather than take the job down."""
    led = TokenLedger()
    led.estimate("sys", [{"role": "user", "content": object()}])
    assert led.prompt_tokens > 0 and led.estimated
