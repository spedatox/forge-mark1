"""The validate → permit → execute gauntlet (§4).

A fixed, ordered pipeline. Every failure at every stage becomes an is_error
ToolResult fed back to the model — unknown tool, bad input, permission denial,
and execution crash all look identical to the loop. No exception escapes.

Oversized results are spilled to a file in the Cell workspace and replaced with a
preview + path, so one fat result can't blow the context (§4)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pydantic import ValidationError

from forge.warden.hooks import run_post_tool, run_pre_tool
from forge.warden.permissions import Decision
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

    # 3. Permit (deny → is_error; ask → put it to the operator).
    decision = ctx.permissions.resolve(tool, args, ctx)
    if decision.needs_ask:
        decision = await _ask(tool, name, args, decision, ctx)
    if not decision.allowed:
        return ToolResult(f"Permission denied for {name!r}: {decision.reason}", is_error=True)
    if decision.updated_args is not None:
        try:
            args = tool.Args.model_validate(decision.updated_args)
        except ValidationError as e:
            return ToolResult(f"The permission layer rewrote {name!r}'s input into "
                              f"something invalid: {e}", is_error=True)

    # 3b. pre_tool hooks (Seam 3). Deliberately AFTER permission: a hook must not
    #     be able to see, let alone approve, what the gate refused.
    hooks = getattr(ctx, "hooks", None) or []
    if hooks:
        hooked_args, veto = await run_pre_tool(hooks, tool, args.model_dump(), ctx)
        if veto is not None:
            return ToolResult(f"A hook blocked {name!r}: {veto.reason}", is_error=True)
        try:
            args = tool.Args.model_validate(hooked_args)
        except ValidationError as e:
            # A hook rewrote the arguments into something the tool cannot accept.
            # That is the hook's bug, not the model's, so say so plainly rather
            # than handing the model a validation error it cannot act on.
            return ToolResult(f"A hook produced invalid input for {name!r}: {e}",
                              is_error=True)

    # 4. Execute. Any throw becomes an is_error result (fail loud to the model,
    #    never out of the loop).
    try:
        result = await tool.call(args, ctx)
    except Exception as e:  # noqa: BLE001 — the loop's safety net
        logger.warning("tool_call_raised", extra={"tool": name, "error": repr(e)})
        return ToolResult(f"<tool_error>{type(e).__name__}: {e}</tool_error>", is_error=True)

    # 4b. post_tool hooks (Seam 3), BEFORE capping — a redactor should act on
    #     what was produced, not on a preview of it.
    if hooks:
        result = await run_post_tool(hooks, tool, args.model_dump(), result, ctx)

    # 5. Cap result size — spill oversize to disk (§4). The batch-wide budget is
    #    the engine's job, once every result in the turn is known.
    return await cap_result(tool, name, result, ctx)


async def _ask(tool: Tool, name: str, args: Any, decision: "Decision",
               ctx: ToolContext) -> "Decision":
    """Put a gated action to the operator and turn the answer into a decision.

    With no oracle attached the answer is no — identical to the behaviour before
    any of this existed, which is what makes the ask safe to ship before
    anything is listening for it."""
    from forge.warden.permissions import Decision as _D

    oracle = getattr(ctx, "oracle", None)
    args_dict = args.model_dump() if hasattr(args, "model_dump") else dict(args)
    action_key = ctx.permissions._action_key(args_dict) or name

    if oracle is None:
        return _D("deny", f"{decision.reason} No operator channel is attached, so it "
                          f"cannot be approved while this job runs.", source="gate")

    try:
        answer = await oracle.ask(name, action_key, decision.reason)
    except asyncio.CancelledError:
        raise                      # an interrupt is not an answer
    except Exception as e:  # noqa: BLE001 — an oracle that broke did not say yes
        logger.warning("permission_ask_failed", extra={"tool": name, "error": repr(e)})
        return _D("deny", f"{decision.reason} The approval channel failed.", source="gate")

    if not answer.approved:
        return _D("deny", f"The operator declined this: {answer.note or decision.reason}",
                  source="gate")

    if answer.remember:
        # Records the EXACT action, never a pattern. A glob is a deliberate,
        # hand-written act by an operator reading their own allow-list file; it
        # is not something to infer from one click on one command.
        ctx.permissions.allowlist.add(f"{name}:{action_key}")
    return _D("allow", "approved by the operator", source="gate")


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
