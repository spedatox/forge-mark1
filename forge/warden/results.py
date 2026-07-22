"""Bounding what tool results cost the window (H3).

Two caps, and the second is the one Forge was missing.

**Per result.** A tool declares `max_result_chars`; the harness clamps that to a
system ceiling, so a tool cannot opt into unbounded output by declaring a large
number. Overflow spills to a file in the workspace and the model gets a preview
plus the path.

**Per batch.** One turn can request forty tools. Twenty 19 KB results each pass a
20 KB per-result cap and together bury the window — which is the failure the
per-result cap looks like it prevents and does not. So the batch is checked as a
whole: over budget, the largest results are spilled, biggest first, until the
turn fits.

**The exemption.** `read_file` is exempt, because persisting a read produces a
file whose only use is to be read back — a Read→file→Read loop. The tool bounds
itself instead, through `offset`/`limit` and its own ceiling. Exempt results
still *count* toward the batch total (they occupy the window like anything else);
they are simply never the thing that gets spilled.

Spilled content is not lost. It lands in the workspace, where `grep` and a ranged
`read_file` can reach it — which is what makes spilling a redirection rather than
a truncation.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from forge.warden.tool import Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from forge.model.base import ToolUseRequest

logger = logging.getLogger("forge.warden")

# A tool may declare less than this, never more. Without the clamp, one tool's
# generous self-assessment becomes the whole window's problem.
SYSTEM_MAX_RESULT_CHARS = 50_000

# All of one turn's results together. Generous by design — the point is to catch
# a pathological batch, not to second-guess ordinary work.
MAX_BATCH_RESULT_CHARS = 200_000

# What survives in-band when a result is spilled during the batch pass. Head and
# tail both: a failing build puts the error at the end, and head-only truncation
# is how you lose exactly the line you needed.
PREVIEW_CHARS = 2_000

EXEMPT: float = float("inf")


def effective_cap(tool: Tool | None) -> float:
    """What this tool's results are actually capped at."""
    declared = getattr(tool, "max_result_chars", SYSTEM_MAX_RESULT_CHARS)
    if declared == EXEMPT:
        return EXEMPT
    return min(declared, SYSTEM_MAX_RESULT_CHARS)


def _preview(content: str, keep: int) -> str:
    """Head + tail of `content`, with the omission stated in between."""
    if len(content) <= keep:
        return content
    half = keep // 2
    omitted = len(content) - 2 * half
    return (f"{content[:half]}\n"
            f"…[{omitted} chars omitted from the middle]…\n"
            f"{content[-half:]}")


async def _spill(name: str, content: str, ctx: ToolContext) -> str | None:
    """Write the full result into the workspace. Returns the path, or None if the
    write failed — spilling is best-effort, and a failed spill must still leave
    the caller with a bounded result rather than an exception."""
    path = f".forge_spill/{name}_{abs(hash(content)) & 0xFFFFFF:06x}.txt"
    try:
        await ctx.cell.write(path, content)
        return path
    except Exception:  # noqa: BLE001 — bounding context is the job; the file is a bonus
        logger.warning("result_spill_failed", extra={"tool": name})
        return None


async def cap_result(tool: Tool, name: str, result: ToolResult, ctx: ToolContext) -> ToolResult:
    """Stage one: bound a single result against its own cap.

    Keeps head *and* tail. A failing build or test run puts the thing you need
    at the end, so head-only truncation reliably discards the answer and leaves
    the model to debug blind."""
    limit = effective_cap(tool)
    if len(result.content) <= limit:
        return result
    kept = int(limit)
    path = await _spill(name, result.content, ctx)
    if path:
        note = (f"\n\n…[capped at {kept} chars; the full {len(result.content)} "
                f"chars are at {path} in the workspace — grep it or read it with "
                f"an offset]")
    else:
        note = f"\n\n…[capped at {kept} of {len(result.content)} chars]"
    return ToolResult(_preview(result.content, kept) + note, is_error=result.is_error)


async def enforce_batch_budget(
    tool_uses: "list[ToolUseRequest]",
    results: list[ToolResult],
    tools: dict[str, Tool],
    ctx: ToolContext,
    limit: int = MAX_BATCH_RESULT_CHARS,
) -> list[ToolResult]:
    """Stage two: bound one turn's results as a whole.

    Spills largest-first, because reclaiming the most context per result spilled
    is what keeps the most results intact. Stops as soon as the batch fits."""
    total = sum(len(r.content) for r in results)
    if total <= limit:
        return results

    # Candidates: everything not exempt, largest first.
    order = sorted(
        (i for i, tu in enumerate(tool_uses) if effective_cap(tools.get(tu.name)) != EXEMPT),
        key=lambda i: len(results[i].content),
        reverse=True,
    )

    out = list(results)
    for i in order:
        if total <= limit:
            break
        original = out[i]
        if len(original.content) <= PREVIEW_CHARS:
            continue                      # nothing to reclaim here
        name = tool_uses[i].name
        path = await _spill(name, original.content, ctx)
        where = (f"the full {len(original.content)} chars are at {path} in the "
                 f"workspace — grep it or read it with an offset"
                 if path else f"{len(original.content)} chars dropped")
        replacement = (f"{_preview(original.content, PREVIEW_CHARS)}\n\n"
                       f"…[this turn returned more than the {limit}-char budget for "
                       f"one batch of tool results, so this one was set aside: {where}]")
        total -= len(original.content) - len(replacement)
        out[i] = ToolResult(replacement, is_error=original.is_error)

    return out
