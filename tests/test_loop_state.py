"""The loop as a state machine (§3): named laps, and a failed turn that leaves
no trace in the transcript.

These are the two properties every later recovery path depends on. A retry, a
compaction re-attempt, or a context recovery is a lap with a different name over
a turn that was never committed — if either half is wrong, the recoveries built
on top duplicate text or lose it."""
import asyncio
from typing import Any, AsyncIterator

from pydantic import BaseModel

from forge.model.base import TextDelta
from forge.model.scripted import ScriptedModel, tool_call
from forge.warden.engine import Warden
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import PermissionEngine
from forge.warden.state import ContinueReason, StopReason
from forge.warden.tool import Tool, ToolContext, ToolResult


class NudgeArgs(BaseModel):
    pass


class Nudge(Tool):
    name = "nudge"
    description = "do nothing, successfully"
    Args = NudgeArgs
    is_read_only = True
    is_concurrency_safe = True

    async def call(self, args: NudgeArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult("ok")


class Exploding:
    """A model that streams real text and then loses the connection mid-turn."""
    model_id = "exploding"

    def __init__(self, text: str = "here is my partial answer") -> None:
        self.text = text
        self.calls = 0

    async def stream(self, *, system: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]], signal: asyncio.Event
                     ) -> AsyncIterator[TextDelta]:
        self.calls += 1
        yield TextDelta(self.text)
        raise ConnectionResetError("connection reset by peer")


def _ctx() -> ToolContext:
    return ToolContext(agent_id="t", cell=None, graph=None, files=FileStateCache(),
                       permissions=PermissionEngine(), network_allowed=False)


def _warden(model, **kwargs) -> Warden:
    # Retries off: these tests are about how a failed turn is discarded, not
    # about whether it is re-attempted. Leaving them on would make every failure
    # case sit through a real backoff (see test_retry.py for that behaviour).
    kwargs.setdefault("retry_attempts", 0)
    return Warden(system_prompt="", tools={"nudge": Nudge()}, model=model,
                  ctx=_ctx(), **kwargs)


# ── Named laps ───────────────────────────────────────────────────────────────
def test_every_lap_is_stamped_and_the_terminal_carries_the_path():
    """Two tool turns then a closing turn: three iterations, two laps."""
    steps = [lambda m: ("working", [tool_call("nudge")]),
             lambda m: ("still working", [tool_call("nudge")]),
             lambda m: ("done", [])]
    term = asyncio.run(_warden(ScriptedModel(steps)).run("go"))

    assert term.reason is StopReason.COMPLETED
    assert term.iterations == 3
    assert [t.reason for t in term.transitions] == [ContinueReason.NEXT_TURN] * 2


def test_laps_and_iterations_move_together():
    """The invariant that keeps an unstamped continue site from hiding: the loop
    entered iteration N+1 exactly N times, so it must have recorded N laps."""
    steps = [lambda m: ("x", [tool_call("nudge")])] * 4 + [lambda m: ("done", [])]
    term = asyncio.run(_warden(ScriptedModel(steps)).run("go"))
    assert len(term.transitions) == term.iterations - 1


def test_a_terminal_run_records_no_lap():
    """A single turn that asks for nothing completes without going round."""
    term = asyncio.run(_warden(ScriptedModel([lambda m: ("done", [])])).run("go"))
    assert term.transitions == ()


# ── The discarded turn ───────────────────────────────────────────────────────
def test_a_failed_turn_leaves_nothing_in_the_transcript():
    """Text streamed before the failure reached the operator, but must not reach
    the transcript — otherwise a retry of that turn duplicates it.

    This is what makes recovery-by-retry safe to build: the discard is structural
    (the turn was never appended) rather than a repair applied afterwards."""
    model = Exploding("here is my partial answer")
    term = asyncio.run(_warden(model).run("go"))

    assert term.reason is StopReason.ERROR
    assert "ConnectionResetError" in (term.error or "")
    assert term.messages == [{"role": "user", "content": "go"}]
    assert "partial answer" not in str(term.messages)


def test_the_operator_still_sees_the_partial_text():
    """Discarding the turn is a transcript decision, not a streaming one — the
    deltas were already emitted and stay emitted."""
    events: list[dict] = []
    model = Exploding("half a thought")
    warden = _warden(model, emit=lambda ev: _record(events, ev))
    asyncio.run(warden.run("go"))

    chunks = "".join(e["data"] for e in events if e["type"] == "chunk")
    assert "half a thought" in chunks
    assert any(e["type"] == "error" for e in events)


async def _record(sink: list[dict], event: dict) -> None:
    sink.append(event)
