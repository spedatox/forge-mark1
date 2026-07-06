"""Shell execution tool. Runs inside the Cell only (§9.3) — the Warden never
touches the host shell."""
from __future__ import annotations

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult


class RunCommandArgs(BaseModel):
    command: str = Field(description="The shell command to run inside the sandbox.")
    timeout: int | None = Field(
        default=None, description="Optional per-command wall-clock timeout in seconds.")


class RunCommand(Tool):
    name = "run_command"
    description = (
        "Run a shell command inside your isolated sandbox and get back its stdout, "
        "stderr, and exit code. Use this to build, run tests, execute scripts, install "
        "packages, or inspect the environment. It is NOT read-only and NOT safe to run "
        "in parallel — commands can mutate the workspace, so they run one at a time. "
        "High-blast-radius commands (force-push, recursive delete, piping a download to "
        "a shell) are stopped by the safety gate unless the operator allow-lists them."
    )
    Args = RunCommandArgs
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = False       # individual dangerous commands are caught by the gate

    async def call(self, args: RunCommandArgs, ctx: ToolContext) -> ToolResult:
        res = await ctx.cell.run(args.command, timeout=args.timeout,
                                 env={} if ctx.network_allowed else None)
        parts = [f"exit_code: {res.exit_code}"]
        if res.timed_out:
            parts.append("(timed out)")
        if res.stdout:
            parts.append(f"stdout:\n{res.stdout}")
        if res.stderr:
            parts.append(f"stderr:\n{res.stderr}")
        body = "\n".join(parts)
        return ToolResult(body, is_error=res.exit_code != 0)
