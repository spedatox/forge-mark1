"""Seam 3 — the two extension points inside the dispatch gauntlet.

`dispatch_tool` is five numbered stages. Hooks get exactly two places to stand,
and where they stand is the security design:

**`pre_tool` runs after permission resolution.** A hook cannot see what the gate
denied, and therefore cannot approve it. Placing it earlier would let a plugin
observe — and vote on — an operation the operator's policy already refused,
which inverts the standing law that plugins may tighten and never loosen.

**`post_tool` runs before result capping.** A hook sees the full output, so a
redactor can act on what was actually produced rather than on a preview; the
spill discipline then bounds whatever the hook leaves behind.

Mark II ships an empty list. The cost is one `for` over nothing per call, and
the benefit is that the vocabulary is fixed before anything depends on it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from forge.warden.tool import ToolResult

if TYPE_CHECKING:
    from forge.warden.tool import Tool, ToolContext

logger = logging.getLogger("forge.warden")


@dataclass(frozen=True)
class HookVerdict:
    """A hook's opinion before a tool runs.

    `updated_args` is the channel that lets a hook correct a call rather than
    only refuse it — rewriting a path, scoping a command. Refusal is blunt and
    costs the model a turn to work around; correction is usually what was
    actually wanted."""
    allow: bool = True
    reason: str = ""
    updated_args: dict[str, Any] | None = None


@runtime_checkable
class ToolHook(Protocol):
    name: str

    async def pre_tool(self, tool: "Tool", args: dict[str, Any],
                       ctx: "ToolContext") -> HookVerdict | None:
        """After permit, before execute. None means no opinion."""
        ...

    async def post_tool(self, tool: "Tool", args: dict[str, Any], result: ToolResult,
                        ctx: "ToolContext") -> ToolResult:
        """After execute, before capping. Returns the result to carry forward."""
        ...


async def run_pre_tool(
    hooks: list[ToolHook], tool: "Tool", args: dict[str, Any], ctx: "ToolContext"
) -> tuple[dict[str, Any], HookVerdict | None]:
    """Consult each hook in order. The first refusal wins and the rest are
    skipped — once an operation is refused, later opinions cannot un-refuse it,
    and asking for them would only invite the expectation that they might.

    A hook that raises is logged and ignored. A broken observer must not be able
    to deny work the policy allows, and fail-closed here would hand any plugin
    an accidental veto over the whole toolset."""
    for hook in hooks:
        try:
            verdict = await hook.pre_tool(tool, args, ctx)
        except Exception as e:  # noqa: BLE001 — see docstring
            logger.warning("pre_tool_hook_failed",
                           extra={"hook": hook.name, "tool": tool.name, "error": repr(e)})
            continue
        if verdict is None:
            continue
        if verdict.updated_args is not None:
            args = verdict.updated_args
        if not verdict.allow:
            return args, verdict
    return args, None


async def run_post_tool(
    hooks: list[ToolHook], tool: "Tool", args: dict[str, Any],
    result: ToolResult, ctx: "ToolContext"
) -> ToolResult:
    """Pass the result through each hook in order. A hook that raises leaves the
    result as it was — a failed redactor must not delete the output."""
    for hook in hooks:
        try:
            result = await hook.post_tool(tool, args, result, ctx)
        except Exception as e:  # noqa: BLE001 — see docstring
            logger.warning("post_tool_hook_failed",
                           extra={"hook": hook.name, "tool": tool.name, "error": repr(e)})
    return result
