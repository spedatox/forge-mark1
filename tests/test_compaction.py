"""Reclaiming context (H5/H6).

The load-bearing test here is `test_the_cut_never_orphans_a_tool_use`: separating
a tool_use from its tool_result produces a transcript the API rejects outright,
turning a rescue into a job-ending error. It is prevented by where the cut is
allowed to land, and this asserts that over randomized shapes rather than the one
transcript that happened to be convenient.
"""
import asyncio
import random
from typing import Any, AsyncIterator

import pytest
from pydantic import BaseModel

from forge.model.base import TextDelta, UsageReport
from forge.warden.compaction import (
    ELIDED,
    elide_old_tool_results,
    find_cut,
    is_tool_result_message,
    opens_a_cycle,
    rebuild,
    render_for_summary,
)
from forge.warden.engine import Warden
from forge.warden.filestate import FileStateCache
from forge.warden.ledger import TokenLedger
from forge.warden.permissions import PermissionEngine
from forge.warden.state import ContinueReason, StopReason
from forge.warden.tool import Tool, ToolContext, ToolResult


# ── Synthetic transcripts ────────────────────────────────────────────────────
def make_transcript(cycles: int, rng: random.Random | None = None) -> list[dict[str, Any]]:
    """A well-formed transcript: task, then `cycles` complete tool cycles."""
    rng = rng or random.Random(0)
    messages: list[dict[str, Any]] = [{"role": "user", "content": "the original task"}]
    for c in range(cycles):
        n = rng.randint(1, 3)
        ids = [f"toolu_{c}_{k}" for k in range(n)]
        messages.append({"role": "assistant", "content": [
            {"type": "text", "text": f"thinking about step {c}"},
            *({"type": "tool_use", "id": i, "name": "probe", "input": {"n": c}} for i in ids),
        ]})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": i, "content": f"result {i}" * 20,
             "is_error": False} for i in ids
        ]})
    return messages


def assert_well_formed(messages: list[dict[str, Any]]) -> None:
    """Every tool_use is answered by a tool_result in the very next message, and
    no tool_result answers a tool_use that is not there."""
    assert messages, "a transcript must not be empty"
    assert messages[0]["role"] == "user", "the transcript must open with the task"

    open_ids: set[str] = set()
    for i, message in enumerate(messages):
        uses = {b["id"] for b in (message.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "tool_use"}
        results = {b["tool_use_id"] for b in (message.get("content") or [])
                   if isinstance(b, dict) and b.get("type") == "tool_result"}
        if results:
            assert results == open_ids, (
                f"message {i}: results {results} do not answer open uses {open_ids}")
            open_ids = set()
        assert not open_ids or uses == open_ids, f"message {i}: tool_use left unanswered"
        if uses:
            open_ids = uses
    assert not open_ids, "the transcript ends with an unanswered tool_use"

    roles = [m["role"] for m in messages]
    for a, b in zip(roles, roles[1:]):
        assert a != b, f"consecutive {a} messages: the API requires alternation"


def test_the_fixture_itself_is_well_formed():
    """A property test is only as good as the shapes it is given."""
    assert_well_formed(make_transcript(6))


# ── The property that matters ────────────────────────────────────────────────
@pytest.mark.parametrize("seed", range(25))
def test_the_cut_never_orphans_a_tool_use(seed):
    rng = random.Random(seed)
    messages = make_transcript(rng.randint(1, 14), rng)
    cut = find_cut(messages, keep_cycles=rng.randint(0, 6))
    if cut is None:
        return
    assert_well_formed(rebuild(messages, cut, "a summary of what came before"))


def test_the_cut_lands_on_a_cycle_boundary():
    messages = make_transcript(10)
    cut = find_cut(messages, keep_cycles=3)
    assert opens_a_cycle(messages[cut])
    assert is_tool_result_message(messages[cut - 1])


def test_nothing_to_cut_when_the_transcript_is_short():
    assert find_cut(make_transcript(2), keep_cycles=5) is None
    assert find_cut([{"role": "user", "content": "hi"}], keep_cycles=5) is None


# ── What survives ────────────────────────────────────────────────────────────
def test_the_task_survives_and_the_tail_is_verbatim():
    messages = make_transcript(10)
    cut = find_cut(messages, keep_cycles=3)
    out = rebuild(messages, cut, "SUMMARY")

    assert "the original task" in out[0]["content"]
    assert "SUMMARY" in out[0]["content"]
    assert out[1:] == messages[cut:]


def test_the_summary_tells_the_model_its_file_memory_is_stale():
    out = rebuild(make_transcript(10), find_cut(make_transcript(10), 3), "S")
    assert "Re-read any file before editing it" in out[0]["content"]


# ── Elision, the cheap layer ─────────────────────────────────────────────────
def test_elision_replaces_old_results_and_keeps_their_ids():
    messages = make_transcript(8)
    out, freed = elide_old_tool_results(messages, keep_cycles=2)

    assert freed > 0
    assert_well_formed(out)
    elided = [b for m in out for b in (m.get("content") or [])
              if isinstance(b, dict) and b.get("content") == ELIDED]
    assert elided
    assert all(b.get("tool_use_id") for b in elided), "ids must survive or the API 400s"


def test_elision_leaves_recent_cycles_alone():
    messages = make_transcript(8)
    out, _ = elide_old_tool_results(messages, keep_cycles=2)
    assert out[-1] == messages[-1]
    assert out[-3] == messages[-3]


def test_elision_is_idempotent():
    once, freed_a = elide_old_tool_results(make_transcript(8), keep_cycles=2)
    twice, freed_b = elide_old_tool_results(once, keep_cycles=2)
    assert freed_b == 0 and twice == once


def test_elision_no_ops_on_a_short_transcript():
    messages = make_transcript(2)
    out, freed = elide_old_tool_results(messages, keep_cycles=5)
    assert freed == 0 and out == messages


def test_rendering_covers_every_block_kind():
    text = render_for_summary(make_transcript(2))
    assert "the original task" in text
    assert "ASSISTANT calls probe(" in text
    assert "TOOL RESULT" in text


# ── End to end, through the loop ─────────────────────────────────────────────
class ProbeArgs(BaseModel):
    n: int = 0


class Probe(Tool):
    name = "probe"
    description = "returns bulk output"
    Args = ProbeArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True

    async def call(self, args: ProbeArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult("payload " * 500)




def _ctx() -> ToolContext:
    return ToolContext(agent_id="t", cell=None, graph=None, files=FileStateCache(),
                       permissions=PermissionEngine(), network_allowed=False)


class Chatty:
    """Requests tools until told to stop, and reports a full window throughout."""
    model_id = "chatty"

    def __init__(self, turns: int, reported_prompt: int) -> None:
        self.turns = turns
        self.reported = reported_prompt
        self.calls = 0
        self.summaries = 0

    async def stream(self, *, system, messages, tools, signal) -> AsyncIterator[Any]:
        if not tools:                      # the summarization call offers none
            self.summaries += 1
            yield TextDelta("a faithful summary of the earlier work")
            return
        self.calls += 1
        from forge.model.scripted import tool_call
        if self.calls >= self.turns:
            yield TextDelta("done")
        else:
            yield TextDelta(f"turn {self.calls}")
            yield tool_call("probe", n=self.calls)
        # Report a full window every turn. Without this the ledger falls back to
        # its own estimate of a small transcript and the threshold un-trips
        # immediately, so nothing would ever be compacted.
        yield UsageReport(input_tokens=self.reported, output_tokens=100)


def _warden(model, ledger=None, **kw) -> Warden:
    return Warden(system_prompt="", tools={"probe": Probe()}, model=model, ctx=_ctx(),
                  ledger=ledger or TokenLedger(), max_iterations=40, **kw)


def test_a_full_window_triggers_compaction_and_the_job_completes():
    led = TokenLedger(context_limit=200_000, max_output_tokens=16_384)
    led.prompt_tokens = led.compact_at + 1          # already over the line
    model = Chatty(turns=12, reported_prompt=led.compact_at + 1)
    term = asyncio.run(_warden(model, ledger=led).run("go"))

    assert term.reason is StopReason.COMPLETED
    assert_well_formed(term.messages)


def test_compaction_is_announced():
    events: list[dict] = []

    async def sink(ev):
        events.append(ev)

    led = TokenLedger()
    led.prompt_tokens = led.compact_at + 1
    asyncio.run(_warden(Chatty(turns=12, reported_prompt=led.compact_at + 1), ledger=led, emit=sink).run("go"))
    stages = [e["data"]["stage"] for e in events if e["type"] == "compact"]
    assert "elide" in stages


def test_reclaiming_context_drops_read_before_write_grounding():
    """Not only after summarizing. Elision removes tool results, and a read_file
    result is one — so the model can lose a file's contents while the cache
    still reports "you have read this, you may edit it", which permits a blind
    edit against text the model can no longer see."""
    led = TokenLedger()
    led.prompt_tokens = led.compact_at + 1
    warden = _warden(Chatty(turns=12, reported_prompt=led.compact_at + 1), ledger=led)
    warden.ctx.files.record("a.py", "old contents", "hash-1")

    asyncio.run(warden.run("go"))
    assert warden.ctx.files.get("a.py") is None


# ── The reactive path (H6) ───────────────────────────────────────────────────
class Overflowing:
    """Refuses with a context-length error until the transcript gets smaller."""
    model_id = "overflowing"

    def __init__(self) -> None:
        self.calls = 0
        self.compacted = False

    async def stream(self, *, system, messages, tools, signal) -> AsyncIterator[Any]:
        if not tools:
            yield TextDelta("summary of the earlier work")
            return
        self.calls += 1
        if self.calls == 1:
            from forge.model.scripted import tool_call
            yield TextDelta("working")
            yield tool_call("probe", n=1)
            return
        if not self.compacted:
            self.compacted = True
            raise RuntimeError("prompt is too long: 210000 tokens > 200000 maximum")
        yield TextDelta("finished after recovering")


def test_a_context_length_refusal_is_recovered_not_surfaced():
    """It arrives as a 400 and is permanent for the request as sent — but the
    request can be made smaller, which is a different thing from fatal."""
    model = Overflowing()
    term = asyncio.run(_warden(model).run("go"))

    assert term.reason is StopReason.COMPLETED
    assert term.final_text == "finished after recovering"
    assert ContinueReason.RECOVERED_CONTEXT in [t.reason for t in term.transitions]


def test_compaction_gives_up_rather_than_looping_forever():
    """An unbounded compaction retry is a money fire. The reference logged
    sessions retrying thousands of times against a context that could not
    shrink."""
    class AlwaysFull:
        model_id = "always-full"

        def __init__(self) -> None:
            self.summaries = 0

        async def stream(self, *, system, messages, tools, signal):
            if not tools:
                self.summaries += 1
                raise RuntimeError("the summarizer is down too")
                yield  # pragma: no cover
            raise RuntimeError("prompt is too long")
            yield  # pragma: no cover

    model = AlwaysFull()
    term = asyncio.run(_warden(model).run("go"))
    assert term.reason is StopReason.ERROR
    assert model.summaries <= 3
