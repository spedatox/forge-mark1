"""Seam 1 — an MCP server as a tool provider.

Each remote tool is wrapped in a `Tool` whose `call` proxies the protocol. The
engine, the dispatch gauntlet and the permission chain need no changes at all: a
proxied MCP tool is just a tool. That was MARK2_SEAMS' claim about what Seam 1
would buy, and this is what cashing it looks like.

**Names are prefixed** `mcp__{server}__{tool}`. Two servers that both offer
`search` would otherwise collide at the fold, and the fold refuses collisions, so
an operator adding a second server would find their first one stop working.

**Flags fail closed.** A remote tool is not read-only and not concurrency-safe
unless the server's annotations say so. The harness has no way to verify a
server's claim about itself, so the only claims worth honouring are the ones that
restrict.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, create_model

from forge.mcp.client import MCPClient, MCPServerSpec, RemoteTool
from forge.warden.tool import Tool, ToolContext, ToolResult

logger = logging.getLogger("forge.mcp")

_JSON_TO_PYTHON = {"string": str, "number": float, "integer": int,
                   "boolean": bool, "array": list, "object": dict}


def _args_model(tool_name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """Build a pydantic model from a remote tool's JSON Schema.

    Only the top level is translated, and anything unrecognized becomes `Any`.
    Validation here exists to catch the model sending obvious nonsense, not to
    re-implement JSON Schema — the server validates properly, and a strict
    client would reject calls a lenient server would have accepted."""
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        python_type = _JSON_TO_PYTHON.get(spec.get("type"), Any)
        fields[name] = (python_type, ... if name in required else None)
    return create_model(f"{tool_name}Args", __config__=ConfigDict(extra="allow"), **fields)


class MCPTool(Tool):
    """One remote tool, wearing the local contract."""

    def __init__(self, server: str, client: MCPClient, remote: RemoteTool) -> None:
        self._client = client
        self._remote = remote
        self.name = f"mcp__{server}__{remote.name}"
        self.description = remote.description or f"Tool {remote.name!r} from the {server!r} MCP server."
        self.Args = _args_model(self.name, remote.input_schema)
        self._read_only = remote.read_only
        # Read-only AND idempotent is the only combination a server can assert
        # that makes parallel execution safe. Read-only alone still permits a
        # server that serializes badly against itself.
        self._parallel = remote.read_only and remote.idempotent

    def is_read_only(self, args: BaseModel) -> bool:
        return self._read_only

    def is_concurrency_safe(self, args: BaseModel) -> bool:
        return self._parallel

    def schema(self) -> dict[str, Any]:
        # Pass the server's own schema through rather than round-tripping it
        # through pydantic: the remote's description strings and constraints are
        # what the model should see, and our model is a lenient approximation.
        return {"name": self.name, "description": self.description,
                "input_schema": self._remote.input_schema}

    async def call(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        payload = args.model_dump(exclude_none=True)
        text, is_error = await self._client.call_tool(self._remote.name, payload)
        return ToolResult(text, is_error=is_error)


class MCPToolProvider:
    """One configured server. Contributes whatever it turns out to offer."""

    def __init__(self, spec: MCPServerSpec) -> None:
        self.spec = spec
        self.name = f"mcp:{spec.name}"
        self._client: MCPClient | None = None
        self._tools: dict[str, Tool] | None = None

    async def provide(self, cfg: Any, request: Any) -> dict[str, Tool]:
        """Connect on first use, then serve the cached set.

        Re-asking is cheap by design (Seam 1 calls this between turns), and a
        server that failed to start is not retried within a job — a server that
        is down stays down for the ninety seconds a job lasts, and retrying it
        every turn would add a startup timeout to each one."""
        if self._tools is not None:
            return self._tools

        self._client = MCPClient(self.spec)
        if not await self._client.start():
            self._tools = {}
            return self._tools

        try:
            remotes = await self._client.list_tools()
        except Exception as e:  # noqa: BLE001 — degrade to contributing nothing
            logger.warning("mcp_list_tools_failed",
                           extra={"server": self.spec.name, "error": repr(e)})
            remotes = []

        tools: dict[str, Tool] = {}
        for remote in remotes:
            try:
                tool = MCPTool(self.spec.name, self._client, remote)
            except Exception as e:  # noqa: BLE001 — one bad schema, not a bad server
                logger.warning("mcp_tool_unusable",
                               extra={"server": self.spec.name, "tool": remote.name,
                                      "error": repr(e)})
                continue
            tools[tool.name] = tool
        logger.info("mcp_server_ready",
                    extra={"server": self.spec.name, "tools": len(tools)})
        self._tools = tools
        return tools

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
