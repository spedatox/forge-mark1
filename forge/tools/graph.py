"""Codebase-understanding tools, backed by the Graphify sidecar (§5).

These let the agent query a knowledge graph of the codebase instead of re-reading
whole files — the mechanism that reduces reliance on aggressive context compaction
(§5). All three are read-only and concurrency-safe (they only read the graph). If
the graph is unavailable, they return an is_error result telling the model to fall
back to reading files (§4)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult


def _no_graph(ctx: ToolContext) -> ToolResult | None:
    if ctx.graph is None or not getattr(ctx.graph, "available", False):
        reason = getattr(ctx.graph, "unavailable_reason", "no graph indexed for this session")
        return ToolResult(
            f"The codebase graph is unavailable ({reason}). Fall back to reading files "
            f"directly with read_file.", is_error=True)
    return None


class GraphQueryArgs(BaseModel):
    question: str = Field(description="Natural-language question or keyword to search the graph for.")
    mode: str = Field(default="bfs", description="Traversal: 'bfs' (broad) or 'dfs' (deep).")
    depth: int = Field(default=3, description="How many hops to expand from matched nodes.")


class GraphQuery(Tool):
    name = "graph_query"
    description = (
        "Query a knowledge graph of the codebase with a natural-language question and "
        "get back the relevant functions, classes, files, and how they connect — as "
        "compact text context. Use this FIRST to orient yourself instead of reading many "
        "files: 'what calls the auth handler?', 'where is retry logic defined?'. It is "
        "read-only and safe to run in parallel. Returns graph context, not file contents "
        "— follow up with read_file on the specific files it points you to."
    )
    Args = GraphQueryArgs
    is_read_only = True
    is_concurrency_safe = True

    async def call(self, args: GraphQueryArgs, ctx: ToolContext) -> ToolResult:
        if (na := _no_graph(ctx)):
            return na
        try:
            text = await ctx.graph.call("query_graph", {
                "question": args.question, "mode": args.mode, "depth": args.depth})
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"graph query failed: {e}", is_error=True)
        return ToolResult(text or "(no matching nodes in the graph)")


class GraphPathArgs(BaseModel):
    source: str = Field(description="Label of the start node (e.g. a function or class name).")
    target: str = Field(description="Label of the end node.")


class GraphPath(Tool):
    name = "graph_path"
    description = (
        "Find the shortest relationship path between two named entities in the codebase "
        "graph — e.g. how a route handler is connected to a database model. Use it to "
        "understand indirect coupling before making a change. It is read-only and "
        "parallel-safe. Returns the chain of nodes and the edges linking them, or a note "
        "that no path exists."
    )
    Args = GraphPathArgs
    is_read_only = True
    is_concurrency_safe = True

    async def call(self, args: GraphPathArgs, ctx: ToolContext) -> ToolResult:
        if (na := _no_graph(ctx)):
            return na
        try:
            text = await ctx.graph.call("shortest_path", {
                "source": args.source, "target": args.target})
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"graph path lookup failed: {e}", is_error=True)
        return ToolResult(text or "(no path found)")


class GraphOverviewArgs(BaseModel):
    top_n: int = Field(default=10, description="How many of the most-connected nodes to list.")


class GraphOverview(Tool):
    name = "graph_overview"
    description = (
        "Get a high-level map of the codebase: overall graph statistics plus the "
        "'god nodes' — the most-connected functions/classes, which are usually the core "
        "abstractions worth understanding first. Use this at the start of a task on an "
        "unfamiliar codebase. It is read-only and parallel-safe. Returns counts and a "
        "ranked list of central entities."
    )
    Args = GraphOverviewArgs
    is_read_only = True
    is_concurrency_safe = True

    async def call(self, args: GraphOverviewArgs, ctx: ToolContext) -> ToolResult:
        if (na := _no_graph(ctx)):
            return na
        try:
            stats = await ctx.graph.call("graph_stats", {})
            gods = await ctx.graph.call("god_nodes", {"top_n": args.top_n})
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"graph overview failed: {e}", is_error=True)
        return ToolResult(f"{stats}\n{gods}")
