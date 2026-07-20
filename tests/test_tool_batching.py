"""How one turn's tool batch is ordered and parallelized (§4).

The model emits a batch in a deliberate order. The engine may run adjacent
concurrency-safe calls together, but it may never *reorder* across the batch —
the regression these tests exist for is subtle: hoisting every safe call ahead of
every unsafe one, then re-sorting the results back into request order, produces a
transcript that looks correct while the model silently reads pre-write content.
"""
import asyncio

import pytest
from pydantic import BaseModel

from forge.model.scripted import ScriptedModel, tool_call
from forge.warden.engine import Warden
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import PermissionEngine
from forge.warden.state import StopReason
from forge.warden.tool import Tool, ToolContext, ToolResult


# ── A journal the tools mutate and observe, standing in for the Cell ─────────
class _Journal:
    def __init__(self) -> None:
        self.marked = False
        self.seen: list[tuple[str, bool]] = []


class SeeArgs(BaseModel):
    tag: str


class See(Tool):
    """Read-only and parallel-safe — the shape of grep/glob/read_file."""
    name = "see"
    description = "record what the journal looks like right now"
    Args = SeeArgs
    is_read_only = True
    is_concurrency_safe = True

    def __init__(self, journal: _Journal, gate: asyncio.Barrier | None = None) -> None:
        self.journal = journal
        self.gate = gate

    async def call(self, args: SeeArgs, ctx: ToolContext) -> ToolResult:
        if self.gate is not None:
            # Only passable if a sibling call is in flight at the same moment.
            await self.gate.wait()
        else:
            await asyncio.sleep(0.01)   # give a reordering bug room to show itself
        self.journal.seen.append((args.tag, self.journal.marked))
        return ToolResult(args.tag)


class MarkArgs(BaseModel):
    pass


class Mark(Tool):
    """Mutating and not parallel-safe — the shape of write_file/run_command."""
    name = "mark"
    description = "mutate the journal"
    Args = MarkArgs

    def __init__(self, journal: _Journal) -> None:
        self.journal = journal

    async def call(self, args: MarkArgs, ctx: ToolContext) -> ToolResult:
        self.journal.marked = True
        return ToolResult("marked")


def _run(tools: dict[str, Tool], calls: list, **kwargs) -> object:
    steps = [lambda m: ("one batch", calls), lambda m: ("done", [])]
    warden = Warden(
        system_prompt="",
        tools=tools,
        model=ScriptedModel(steps),
        ctx=ToolContext(agent_id="t", cell=None, graph=None, files=FileStateCache(),
                        permissions=PermissionEngine(), network_allowed=False),
        **kwargs,
    )
    return asyncio.run(warden.run("go"))


# ── The regression ───────────────────────────────────────────────────────────
def test_a_read_after_a_write_observes_the_write():
    """`[see, mark, see]` must execute in that order.

    The batch splits into three groups — one safe call, one mutation, one safe
    call — and the trailing read sees the mutation. Under the old
    all-safe-then-all-unsafe partition both reads ran first, concurrently, and
    both reported the pre-write state.
    """
    journal = _Journal()
    term = _run(
        {"see": See(journal), "mark": Mark(journal)},
        [tool_call("see", tag="before"), tool_call("mark"), tool_call("see", tag="after")],
    )
    assert term.reason is StopReason.COMPLETED
    assert journal.seen == [("before", False), ("after", True)]


def test_results_still_come_back_in_request_order():
    """Grouping changes execution order, never the order results are reported."""
    journal = _Journal()
    term = _run(
        {"see": See(journal), "mark": Mark(journal)},
        [tool_call("see", tag="a"), tool_call("mark"), tool_call("see", tag="b")],
    )
    blocks = [b for msg in term.messages if isinstance(msg.get("content"), list)
              for b in msg["content"]
              if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert [b["content"] for b in blocks] == ["a", "marked", "b"]


# ── The property the grouping exists to preserve ─────────────────────────────
def test_consecutive_safe_calls_run_concurrently():
    """Adjacent safe calls share a group. The barrier only opens if both are in
    flight together, so a serialized implementation deadlocks and times out."""
    journal = _Journal()
    gate = asyncio.Barrier(2)
    term = _run(
        {"see": See(journal, gate)},
        [tool_call("see", tag="x"), tool_call("see", tag="y")],
    )
    assert term.reason is StopReason.COMPLETED
    assert {tag for tag, _ in journal.seen} == {"x", "y"}


def test_a_mutation_between_reads_splits_the_group():
    """Same barrier, but a mutation sits between the two reads. They are now in
    different groups, cannot rendezvous, and the barrier must NOT open —
    proving the split is real rather than an artifact of timing."""
    journal = _Journal()
    gate = asyncio.Barrier(2)

    async def scenario():
        warden = Warden(
            system_prompt="",
            tools={"see": See(journal, gate), "mark": Mark(journal)},
            model=ScriptedModel([
                lambda m: ("batch", [tool_call("see", tag="x"), tool_call("mark"),
                                     tool_call("see", tag="y")]),
                lambda m: ("done", []),
            ]),
            ctx=ToolContext(agent_id="t", cell=None, graph=None, files=FileStateCache(),
                            permissions=PermissionEngine(), network_allowed=False),
        )
        await warden.run("go")

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(scenario(), timeout=1.0))


def test_in_flight_tools_are_capped():
    """The cap bounds the Cell: a 40-call sweep must not open 40 subprocesses."""
    journal = _Journal()
    peak = 0
    live = 0

    class Counting(See):
        async def call(self, args, ctx):
            nonlocal peak, live
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1
            return ToolResult(args.tag)

    _run({"see": Counting(journal)},
         [tool_call("see", tag=str(i)) for i in range(40)],
         max_tool_concurrency=4)
    assert peak == 4
