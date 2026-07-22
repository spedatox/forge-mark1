"""A throwaway out-of-tree extension, and the reason it is in the tree.

This module exercises four seams — tool provision, event sinks, prompt
fragments, and dispatch hooks — using **only the public registration surfaces**.
It imports the `Tool` contract, the fragment type, and nothing else from
`forge`: no engine internals, no `ALL_TOOLS`, no reaching into `run_job`.

That restraint is the entire point. A seam nothing has ever gone through is a
guess about what an extension will need, and guesses are discovered to be wrong
at exactly the moment a plugin architecture is being built on them. If this
probe ever needs an import that is not a public surface, the seam it reached
past was violated, and it should be fixed rather than the probe loosened.

It is not shipped, enabled, or referenced by any profile. Its only consumer is
the conformance test.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from forge.agents.prompt import PromptFragment
from forge.warden.hooks import HookVerdict
from forge.warden.tool import Tool, ToolContext, ToolResult


# ── Seam 1: a tool from outside the package ──────────────────────────────────
class TideArgs(BaseModel):
    port: str = Field(description="Which port to report the tide for.")


class Tide(Tool):
    name = "probe_tide"
    description = (
        "A probe tool contributed by an out-of-tree provider. It reports a fixed "
        "answer and exists only to prove that a tool can reach the model without "
        "being registered in the Forge's own tool package."
    )
    Args = TideArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True

    async def call(self, args: TideArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult(f"The tide at {args.port} is coming in.")


class ProbeToolProvider:
    """A second provider, alongside the builtin one."""

    name = "plugin_probe"

    def __init__(self) -> None:
        self.provide_calls = 0
        self.closed = False

    async def provide(self, cfg: Any, request: Any) -> dict[str, Tool]:
        self.provide_calls += 1
        return {Tide.name: Tide()}

    async def close(self) -> None:
        self.closed = True


# ── Seam 3: a hook that observes and does not interfere ──────────────────────
class ProbeHook:
    name = "plugin_probe"

    def __init__(self) -> None:
        self.pre: list[str] = []
        self.post: list[str] = []

    async def pre_tool(self, tool: Tool, args: dict[str, Any],
                       ctx: ToolContext) -> HookVerdict | None:
        self.pre.append(tool.name)
        return None                      # no opinion

    async def post_tool(self, tool: Tool, args: dict[str, Any],
                        result: ToolResult, ctx: ToolContext) -> ToolResult:
        self.post.append(tool.name)
        return result


# ── Seam 4: an event sink ────────────────────────────────────────────────────
class ProbeSink:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def __call__(self, event: Any) -> None:
        self.events.append(event.type)


# ── Seam 7: a prompt fragment ────────────────────────────────────────────────
FRAGMENT = PromptFragment(
    source="skill:probe",
    text="When asked about tides, use the probe_tide tool rather than guessing.",
)
