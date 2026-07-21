"""A minimal MCP client over stdio (H10).

Enough of the protocol to do the one thing Forge needs: discover a server's
tools and call them. `initialize`, `tools/list`, `tools/call`. No resources, no
prompts, no sampling — those are surfaces Forge has no consumer for, and an
unused implementation is a maintenance cost with no upside.

Written against the wire format rather than the MCP SDK. The SDK is a large
dependency for three request types, and the Forge's install has stayed small
enough that `pydantic`, `websockets` and `anthropic` is the whole of it.

Everything here fails soft. A server that will not start, answers slowly, or
returns nonsense degrades to "that server contributed no tools" — a broken
integration must not be able to stop a job that never needed it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("forge.mcp")

PROTOCOL_VERSION = "2025-06-18"
STARTUP_TIMEOUT_S = 20.0
CALL_TIMEOUT_S = 120.0
_MAX_LINE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class MCPServerSpec:
    """How to start one server."""
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RemoteTool:
    name: str                       # as the server calls it
    description: str
    input_schema: dict[str, Any]
    read_only: bool = False         # only ever true when the server says so
    idempotent: bool = False


class MCPClient:
    """One stdio server connection."""

    def __init__(self, spec: MCPServerSpec) -> None:
        self.spec = spec
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> bool:
        """Spawn and handshake. False if the server is unusable."""
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.spec.command, *self.spec.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={**self.spec.env} or None,
                limit=_MAX_LINE_BYTES,
            )
            reply = await asyncio.wait_for(self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "forge", "version": "2.0"},
            }), timeout=STARTUP_TIMEOUT_S)
            if reply is None:
                raise RuntimeError("no initialize reply")
            await self._notify("notifications/initialized", {})
            return True
        except Exception as e:  # noqa: BLE001 — a bad server is not a bad job
            logger.warning("mcp_server_start_failed",
                           extra={"server": self.spec.name, "error": repr(e)})
            await self.close()
            return False

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, ProcessLookupError, OSError):
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass

    # ── The three calls Forge makes ──────────────────────────────────────────
    async def list_tools(self) -> list[RemoteTool]:
        reply = await self._request("tools/list", {})
        tools: list[RemoteTool] = []
        for entry in (reply or {}).get("tools", []) or []:
            name = entry.get("name")
            if not name:
                continue
            # Annotations are hints from the server and are trusted only in the
            # restricting direction: absent means "assume it writes". A server
            # that forgets to declare readOnlyHint costs itself parallelism; one
            # that could claim safety by silence would cost the workspace.
            hints = entry.get("annotations") or {}
            tools.append(RemoteTool(
                name=name,
                description=(entry.get("description") or "").strip(),
                input_schema=entry.get("inputSchema") or {"type": "object"},
                read_only=bool(hints.get("readOnlyHint", False)),
                idempotent=bool(hints.get("idempotentHint", False)),
            ))
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Returns (rendered text, is_error)."""
        try:
            reply = await asyncio.wait_for(
                self._request("tools/call", {"name": name, "arguments": arguments}),
                timeout=CALL_TIMEOUT_S)
        except asyncio.TimeoutError:
            return (f"The MCP server {self.spec.name!r} did not answer within "
                    f"{CALL_TIMEOUT_S:.0f}s."), True
        if reply is None:
            return f"The MCP server {self.spec.name!r} returned no result.", True

        parts: list[str] = []
        for block in reply.get("content", []) or []:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                # Images and embedded resources have no place in a text-only
                # tool result; name what arrived rather than dropping it silently.
                parts.append(f"[{block.get('type', 'unknown')} content omitted]")
        return "\n".join(parts) or "(the server returned no content)", \
            bool(reply.get("isError", False))

    # ── Wire ─────────────────────────────────────────────────────────────────
    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """One JSON-RPC round trip.

        Serialized on a lock: request ids are matched by reading the next reply,
        so two overlapping calls on one pipe would race for each other's
        answers. MCP servers are not a throughput path here."""
        async with self._lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.stdout is None:
                return None
            self._next_id += 1
            request_id = self._next_id
            payload = {"jsonrpc": "2.0", "id": request_id,
                       "method": method, "params": params}
            try:
                proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
                await proc.stdin.drain()
            except (OSError, RuntimeError) as e:
                logger.warning("mcp_write_failed",
                               extra={"server": self.spec.name, "error": repr(e)})
                return None

            # Skip notifications and any reply that is not ours.
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return None                    # the server went away
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue                       # servers log to stdout sometimes
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    logger.warning("mcp_error_reply",
                                   extra={"server": self.spec.name,
                                          "error": str(message["error"])[:300]})
                    return None
                return message.get("result") or {}

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(
                (json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
                .encode("utf-8"))
            await proc.stdin.drain()
        except (OSError, RuntimeError):
            pass
