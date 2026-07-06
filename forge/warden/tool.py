"""The tool boundary (§4).

A tool the model sees is exactly three things: `name`, `description`, and an
input schema (a Pydantic model → JSON Schema, the study's Zod→Pydantic mapping).
Everything else — read-only? concurrency-safe? destructive? result-size cap?
permissions? — is harness-side and invisible to the model.

Fail-closed defaults (§4): a new tool is assumed NOT concurrency-safe, NOT
read-only, NOT destructive, and NOT auto-permitted unless it declares otherwise.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from forge.cell.base import Cell
    from forge.graph.sidecar import GraphSidecar
    from forge.warden.permissions import Decision, PermissionEngine
    from forge.warden.filestate import FileStateCache


@dataclass
class ToolResult:
    """What a tool hands back to the loop. `is_error` is the uniform shape for
    every failure at every stage (§4) — the model reads it and adapts."""
    content: str
    is_error: bool = False


@dataclass
class ToolContext:
    """Harness-side execution context. Never serialized to the model."""
    agent_id: str
    cell: "Cell"
    graph: "GraphSidecar | None"
    files: "FileStateCache"
    permissions: "PermissionEngine"
    network_allowed: bool


class Tool(abc.ABC):
    # ── Model-facing (the entire contract the model sees) ────────────────────
    name: str
    description: str
    Args: type[BaseModel]            # the input schema

    # ── Harness-side, fail-closed defaults (§4) ──────────────────────────────
    is_read_only: bool = False       # assume it writes
    is_concurrency_safe: bool = False  # assume unsafe to parallelize
    is_destructive: bool = False     # assume reversible; destructive tools opt in
    max_result_chars: int = 20_000   # cap one result; oversize is truncated/spilled

    @abc.abstractmethod
    async def call(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """Do the work. May raise — the dispatcher converts any throw into an
        is_error result, so an exception never escapes the loop (§4)."""

    def check_permissions(self, args: BaseModel, ctx: ToolContext) -> "Decision | None":
        """Tool-specific permission opinion (e.g. shell subcommand rules).
        None = defer to the general precedence chain. Overridden by tools that
        need finer control than their default classification provides."""
        return None

    def schema(self) -> dict[str, Any]:
        """The model-facing tool definition — name, description, input schema."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.Args.model_json_schema(),
        }
