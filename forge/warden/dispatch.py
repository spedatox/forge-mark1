"""The validate → permit → execute gauntlet (§4).

A fixed, ordered pipeline. Every failure at every stage becomes an is_error
ToolResult fed back to the model — unknown tool, bad input, permission denial,
and execution crash all look identical to the loop. No exception escapes.

Oversized results are spilled to a file in the Cell workspace and replaced with a
preview + path, so one fat result can't blow the context (§4)."""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from forge.warden.tool import Tool, ToolContext, ToolResult

logger = logging.getLogger("forge.warden")


async def dispatch_tool(
    tools: dict[str, Tool],
    name: str,
    raw_input: dict[str, Any],
    ctx: ToolContext,
) -> ToolResult:
    """Run one tool call through the full gauntlet, returning a ToolResult that is
    always safe to hand back to the model."""

    # 1. Resolve the tool. Unknown → correctable error, not a crash.
    tool = tools.get(name)
    if tool is None:
        available = ", ".join(sorted(tools)) or "(none)"
        return ToolResult(f"Unknown tool {name!r}. Available tools: {available}.", is_error=True)

    # 2. Validate input against the schema.
    try:
        args = tool.Args.model_validate(raw_input)
    except ValidationError as e:
        return ToolResult(f"Invalid input for {name!r}: {e}", is_error=True)

    args_dict = args.model_dump()

    # 3. Permit (deny/gate → is_error).
    decision = ctx.permissions.resolve(tool, args_dict, ctx)
    if not decision.allowed:
        return ToolResult(f"Permission denied for {name!r}: {decision.reason}", is_error=True)

    # 4. Execute. Any throw becomes an is_error result (fail loud to the model,
    #    never out of the loop).
    try:
        result = await tool.call(args, ctx)
    except Exception as e:  # noqa: BLE001 — the loop's safety net
        logger.warning("tool_call_raised", extra={"tool": name, "error": repr(e)})
        return ToolResult(f"<tool_error>{type(e).__name__}: {e}</tool_error>", is_error=True)

    # 5. Cap result size — spill oversize to disk (§4).
    return await _cap_result(tool, name, result, ctx)


async def _cap_result(tool: Tool, name: str, result: ToolResult, ctx: ToolContext) -> ToolResult:
    limit = tool.max_result_chars
    if len(result.content) <= limit:
        return result
    preview = result.content[:limit]
    spill_path = f".forge_spill/{name}_{abs(hash(result.content)) & 0xFFFFFF:06x}.txt"
    try:
        await ctx.cell.write(spill_path, result.content)
        note = (f"\n\n…[result truncated at {limit} chars; full "
                f"{len(result.content)} chars written to {spill_path} in the Cell "
                f"— read it there if you need the rest]")
    except Exception:  # noqa: BLE001 — spilling is best-effort; truncation still bounds context
        note = f"\n\n…[result truncated at {limit} of {len(result.content)} chars]"
    return ToolResult(preview + note, is_error=result.is_error)


def to_anthropic_tool_result(tool_use_id: str, result: ToolResult) -> dict[str, Any]:
    """Render a ToolResult as an Anthropic tool_result content block."""
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": result.content,
        "is_error": result.is_error,
    }


def _debug_dump(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str)[:500]
    except Exception:  # noqa: BLE001
        return repr(obj)[:500]
