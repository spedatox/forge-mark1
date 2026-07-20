"""Bounding tool results (H3): the per-result cap, and the batch budget that a
per-result cap cannot see."""
import asyncio

import pytest
from pydantic import BaseModel

from forge.cell.base import CellPolicy
from forge.cell.subprocess_cell import SubprocessCell
from forge.model.scripted import ScriptedModel, tool_call
from forge.tools.files import MAX_READ_CHARS, ReadFile, ReadFileArgs
from forge.warden.engine import Warden
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import PermissionEngine
from forge.warden.results import (
    EXEMPT,
    MAX_BATCH_RESULT_CHARS,
    SYSTEM_MAX_RESULT_CHARS,
    cap_result,
    effective_cap,
)
from forge.warden.tool import Tool, ToolContext, ToolResult


class BulkArgs(BaseModel):
    size: int


class Bulk(Tool):
    """Returns exactly as many characters as it is asked for."""
    name = "bulk"
    description = "produce output of a requested size"
    Args = BulkArgs
    is_read_only = True
    is_concurrency_safe = True
    max_result_chars = 40_000

    async def call(self, args: BulkArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult("x" * args.size)


class Greedy(Bulk):
    """A tool that would like no cap at all."""
    name = "greedy"
    max_result_chars = 10_000_000


@pytest.fixture
def ctx(tmp_path):
    cell = SubprocessCell(workspace=tmp_path, policy=CellPolicy())
    asyncio.run(cell.start())
    return ToolContext(agent_id="t", cell=cell, graph=None, files=FileStateCache(),
                       permissions=PermissionEngine(), network_allowed=False)


def _warden(steps, tools, ctx) -> Warden:
    return Warden(system_prompt="", tools=tools, model=ScriptedModel(steps), ctx=ctx)


def _results(term) -> list[str]:
    return [b["content"] for msg in term.messages if isinstance(msg.get("content"), list)
            for b in msg["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"]


# ── Per-result cap ───────────────────────────────────────────────────────────
def test_a_tool_cannot_declare_its_way_past_the_system_ceiling():
    assert effective_cap(Bulk()) == 40_000
    assert effective_cap(Greedy()) == SYSTEM_MAX_RESULT_CHARS
    assert effective_cap(ReadFile()) == EXEMPT


def test_the_per_result_cap_keeps_the_tail(ctx):
    """Regression: head-only truncation discarded the end of every oversize
    result, so a failing build's error — which lives at the end — never reached
    the model, and the batch pass below could not recover what stage one had
    already thrown away."""
    class Marked(Bulk):
        async def call(self, args, ctx):
            return ToolResult("HEAD" + ("x" * (args.size - 8)) + "TAIL")

    tool = Marked()
    res = asyncio.run(tool.call(BulkArgs(size=120_000), ctx))
    capped = asyncio.run(cap_result(tool, "bulk", res, ctx))
    assert capped.content.startswith("HEAD")
    assert "TAIL" in capped.content


def test_an_oversize_result_is_capped_and_spilled(ctx, tmp_path):
    steps = [lambda m: ("go", [tool_call("bulk", size=120_000)]), lambda m: ("done", [])]
    term = asyncio.run(_warden(steps, {"bulk": Bulk()}, ctx).run("go"))
    [out] = _results(term)
    assert len(out) < 50_000
    assert ".forge_spill/" in out
    spilled = list((tmp_path / ".forge_spill").glob("bulk_*.txt"))
    assert spilled and len(spilled[0].read_text(encoding="utf-8")) == 120_000


# ── The batch budget ─────────────────────────────────────────────────────────
def test_many_results_each_within_cap_still_get_bounded(ctx):
    """The failure a per-result cap looks like it prevents and does not: twenty
    39K results each pass their own cap and together bury the window."""
    calls = [tool_call("bulk", size=39_000) for _ in range(20)]
    steps = [lambda m: ("go", calls), lambda m: ("done", [])]
    term = asyncio.run(_warden(steps, {"bulk": Bulk()}, ctx).run("go"))

    out = _results(term)
    assert len(out) == 20
    assert sum(len(c) for c in out) <= MAX_BATCH_RESULT_CHARS


def test_the_batch_spills_largest_first(ctx):
    """Reclaiming the most context per result spilled keeps the most results
    intact — the small ones should survive untouched."""
    calls = [tool_call("bulk", size=n) for n in (45_000, 45_000, 45_000, 45_000, 45_000, 300)]
    steps = [lambda m: ("go", calls), lambda m: ("done", [])]
    term = asyncio.run(_warden(steps, {"bulk": Bulk()}, ctx).run("go"))

    out = _results(term)
    assert out[-1] == "x" * 300                     # the small one is untouched
    assert any("set aside" in c for c in out[:-1])  # a large one was not


def test_a_batch_within_budget_is_left_alone(ctx):
    calls = [tool_call("bulk", size=1_000) for _ in range(5)]
    steps = [lambda m: ("go", calls), lambda m: ("done", [])]
    term = asyncio.run(_warden(steps, {"bulk": Bulk()}, ctx).run("go"))
    assert all(c == "x" * 1_000 for c in _results(term))


def test_a_spilled_result_keeps_head_and_tail(ctx):
    """A failing build puts the error at the end. Head-only truncation is how
    you lose exactly the line you needed."""
    class Marked(Bulk):
        async def call(self, args, ctx):
            body = "HEAD" + ("x" * (args.size - 8)) + "TAIL"
            return ToolResult(body)

    calls = [tool_call("bulk", size=49_000) for _ in range(6)]
    steps = [lambda m: ("go", calls), lambda m: ("done", [])]
    term = asyncio.run(_warden(steps, {"bulk": Marked()}, ctx).run("go"))

    setaside = [c for c in _results(term) if "set aside" in c]
    assert setaside
    assert setaside[0].startswith("HEAD") and "TAIL" in setaside[0]


def test_reads_are_never_the_thing_spilled(ctx, tmp_path):
    """Spilling a read produces a file whose only use is to be read back."""
    (tmp_path / "big.txt").write_text("line\n" * 5_000, encoding="utf-8")
    calls = ([tool_call("bulk", size=49_000) for _ in range(5)]
             + [tool_call("read_file", path="big.txt")])
    steps = [lambda m: ("go", calls), lambda m: ("done", [])]
    term = asyncio.run(_warden(steps, {"bulk": Bulk(), "read_file": ReadFile()}, ctx).run("go"))

    out = _results(term)
    assert "set aside" not in out[-1]
    assert "1→ line" in out[-1]
    assert any("set aside" in c for c in out[:-1])


# ── read_file bounds itself, which is what makes the exemption safe ──────────
def test_an_oversize_read_refuses_rather_than_truncating(ctx, tmp_path):
    """A refusal costs ~100 chars and the model retries narrower; a silent
    truncation costs the full ceiling AND hides that there was more."""
    (tmp_path / "huge.txt").write_text("y" * 80 + "\n", encoding="utf-8")
    with (tmp_path / "huge.txt").open("w", encoding="utf-8") as fh:
        fh.write(("y" * 200 + "\n") * 2_000)

    res = asyncio.run(ReadFile().call(ReadFileArgs(path="huge.txt"), ctx))
    assert res.is_error
    assert "ceiling for one read" in res.content and "limit=" in res.content
    assert len(res.content) < 400


def test_a_single_monstrous_line_is_cut_rather_than_refused(ctx, tmp_path):
    """No smaller limit can help here, so refusing would be a dead end."""
    (tmp_path / "min.js").write_text("z" * (MAX_READ_CHARS + 5_000), encoding="utf-8")
    res = asyncio.run(ReadFile().call(ReadFileArgs(path="min.js"), ctx))
    assert not res.is_error
    assert "cut at" in res.content
