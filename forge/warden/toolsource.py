"""Seam 1 — where a job's tools come from.

`ALL_TOOLS` is a dict in a package, so a tool that lives anywhere else — an MCP
server's, a plugin's — has nowhere to stand. This is the standing-place: an
ordered list of providers, folded at job assembly.

**Collisions are loud.** A later provider may not shadow an earlier provider's
name. Silent override is how a plugin quietly replaces `write_file` with its own
and nobody finds out until something is deleted; the alternative to a startup
error is a security incident.

**Providers are re-callable.** `provide` is asked again between turns, not only
once at assembly, because an MCP server that finishes connecting mid-job would
otherwise be unusable until the next job. The fold is cheap and idempotent for
the builtin provider, so re-asking costs nothing when nothing changed.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from forge.warden.tool import Tool

if TYPE_CHECKING:
    from forge.agents.config import AgentConfig
    from forge.gate.protocol import JobRequest

logger = logging.getLogger("forge.warden")


@runtime_checkable
class ToolProvider(Protocol):
    """A source of tools for one job."""

    name: str

    async def provide(self, cfg: "AgentConfig", request: "JobRequest") -> dict[str, Tool]:
        """The tools this source contributes. Called at assembly and may be
        called again between turns; it must be safe to call repeatedly."""
        ...

    async def close(self) -> None:
        """Release anything held open. Always called, even when the job fails."""
        ...


class BuiltinToolProvider:
    """The curated set, filtered by the profile's allowlist.

    Behaviour is exactly what `run_job` used to do inline. It exists as a
    provider so that the builtin tools arrive through the same door as
    everything else — a seam with one implementation is a guess, and a seam
    whose only user bypasses it is decoration."""

    name = "builtin"

    def __init__(self, table: dict[str, type[Tool]] | None = None) -> None:
        if table is None:
            from forge.tools import ALL_TOOLS
            table = ALL_TOOLS
        self._table = table

    async def provide(self, cfg: "AgentConfig", request: "JobRequest") -> dict[str, Tool]:
        missing = [n for n in cfg.tool_names if n not in self._table]
        if missing:
            raise KeyError(
                f"agent {cfg.agent_id!r} allowlists unknown tool(s): {', '.join(missing)}. "
                f"Known: {', '.join(sorted(self._table))}.")
        return {name: self._table[name]() for name in cfg.tool_names}

    async def close(self) -> None:
        return None


async def fold_providers(
    providers: list[ToolProvider], cfg: "AgentConfig", request: "JobRequest"
) -> dict[str, Tool]:
    """Merge every provider's contribution, in order, refusing collisions.

    A provider that raises is fatal at assembly: a job whose toolset silently
    lost a source would behave like a differently-configured agent, and the
    model would spend its iterations discovering that a tool it was told about
    does not exist."""
    tools: dict[str, Tool] = {}
    owners: dict[str, str] = {}
    for provider in providers:
        contributed = await provider.provide(cfg, request)
        for name, tool in contributed.items():
            if name in tools:
                raise ValueError(
                    f"tool name collision: {provider.name!r} provides {name!r}, "
                    f"which {owners[name]!r} already provided. Names must be unique "
                    f"across providers — a later source may never shadow an earlier "
                    f"one, because a silently replaced tool is indistinguishable "
                    f"from the real thing.")
            tools[name] = tool
            owners[name] = provider.name
    return tools


async def close_providers(providers: list[ToolProvider]) -> None:
    """Close every provider, surviving individual failures — one badly-behaved
    source must not strand the rest."""
    for provider in providers:
        try:
            await provider.close()
        except Exception:  # noqa: BLE001 — teardown is best-effort by nature
            logger.warning("tool_provider_close_failed", extra={"provider": provider.name})
