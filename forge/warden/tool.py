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
    # Declared as constants because most tools have one honest answer for every
    # input. Read through the methods below, never directly: a tool whose answer
    # depends on its arguments — a shell that is read-only for `ls` and not for
    # `rm` — overrides the method, and every call site must reach that override.
    READ_ONLY: bool = False          # assume it writes
    CONCURRENCY_SAFE: bool = False   # assume unsafe to parallelize
    DESTRUCTIVE: bool = False        # assume reversible; destructive tools opt in

    max_result_chars: float = 20_000  # cap one result; oversize is truncated/spilled

    def is_read_only(self, args: BaseModel) -> bool:
        return self.READ_ONLY

    def is_concurrency_safe(self, args: BaseModel) -> bool:
        """Whether THIS call may run alongside others. Per-input, because the
        answer usually is: two greps are safe together, two `pip install`s are
        not, and a tool that must answer for its worst case serializes its best
        one."""
        return self.CONCURRENCY_SAFE

    def is_destructive(self, args: BaseModel) -> bool:
        return self.DESTRUCTIVE

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject a subclass that shadows a safety method with a plain value.

        These were class attributes before they were methods, so `is_read_only =
        True` still *looks* right. It silently replaces the method with a bool,
        every call site's `flag(args)` raises, and each one fails closed — the
        tool keeps working while quietly losing parallelism or gaining a gate.
        Failing closed is what makes this invisible, so it has to be caught here
        rather than discovered as a mysterious slowdown."""
        super().__init_subclass__(**kwargs)
        for name, constant in (("is_read_only", "READ_ONLY"),
                               ("is_concurrency_safe", "CONCURRENCY_SAFE"),
                               ("is_destructive", "DESTRUCTIVE")):
            value = cls.__dict__.get(name)
            if value is not None and not callable(value):
                raise TypeError(
                    f"{cls.__name__}.{name} is set to {value!r}, but it is a method. "
                    f"Declare `{constant} = {value!r}` instead, or override "
                    f"`{name}(self, args)` if the answer depends on the input.")

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
