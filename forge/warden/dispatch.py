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

from forge.warden.results import cap_result
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

    # 3. Permit (deny/gate → is_error).
    decision = ctx.permissions.resolve(tool, args, ctx)
    if not decision.allowed:
        return ToolResult(f"Permission denied for {name!r}: {decision.reason}", is_error=True)

    # 4. Execute. Any throw becomes an is_error result (fail loud to the model,
    #    never out of the loop).
    try:
        result = await tool.call(args, ctx)
    except Exception as e:  # noqa: BLE001 — the loop's safety net
        logger.warning("tool_call_raised", extra={"tool": name, "error": repr(e)})
        return ToolResult(f"<tool_error>{type(e).__name__}: {e}</tool_error>", is_error=True)

    # 5. Cap result size — spill oversize to disk (§4). The batch-wide budget is
    #    the engine's job, once every result in the turn is known.
    return await cap_result(tool, name, result, ctx)


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
