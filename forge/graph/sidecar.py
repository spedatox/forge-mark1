"""GraphSidecar — a minimal MCP stdio client for Graphify's server mode (§5).

Lifecycle:
    1. index once: `python -m graphify <repo> --no-label` → <repo>/graphify-out/graph.json
       (pure tree-sitter AST extraction; API keys are stripped from the build env
        so it never makes an LLM call, and --no-label skips community naming).
    2. serve:      `python -m graphify.serve <graph.json> --transport stdio`
    3. speak MCP:  initialize → notifications/initialized → tools/call

Graphify's MCP tools (probed from the running server): query_graph, get_node,
get_neighbors, get_community, god_nodes, graph_stats, shortest_path. The Warden
exposes a curated subset to the agent (see forge/tools/graph.py).

If indexing or the server handshake fails, the sidecar reports unavailable and
the graph tool returns an is_error result the model can react to (§4) — a missing
graph never crashes a job.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("forge.graph")

_SECRET_ENV = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
               "ZAI_API_KEY", "DEEPSEEK_API_KEY")


class GraphSidecar:
    def __init__(self, repo_path: Path, *, index_timeout_s: int = 180,
                 request_timeout_s: int = 30) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.graph_json = self.repo_path / "graphify-out" / "graph.json"
        self.index_timeout_s = index_timeout_s
        self.request_timeout_s = request_timeout_s
        self._proc: asyncio.subprocess.Process | None = None
        self._rpc_id = 0
        self._lock = asyncio.Lock()
        self.available = False
        self.tool_names: list[str] = []
        self.unavailable_reason = "graph sidecar not started"

    # ── Startup ──────────────────────────────────────────────────────────────
    async def start(self) -> bool:
        """Index the repo (once) and launch the MCP server. Returns availability;
        never raises — failure just leaves the sidecar unavailable."""
        try:
            await self._index()
            await self._serve()
            await self._handshake()
            self.available = True
            logger.info("graph_sidecar_ready",
                        extra={"repo": str(self.repo_path), "tools": self.tool_names})
        except Exception as e:  # noqa: BLE001 — a missing graph must not fail the job
            self.unavailable_reason = f"{type(e).__name__}: {e}"
            logger.warning("graph_sidecar_unavailable", extra={"reason": self.unavailable_reason})
            await self.close()
            self.available = False
        return self.available

    async def _index(self) -> None:
        if self.graph_json.exists():
            return  # index once per session; reuse an existing graph
        env = {k: v for k, v in os.environ.items() if k not in _SECRET_ENV}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "graphify", str(self.repo_path), "--no-label",
            cwd=str(self.repo_path), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.index_timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("graphify indexing timed out")
        if proc.returncode != 0 or not self.graph_json.exists():
            tail = (out or b"").decode("utf-8", "replace")[-400:]
            raise RuntimeError(f"graphify indexing failed (rc={proc.returncode}): {tail}")

    async def _serve(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "graphify.serve", str(self.graph_json),
            "--transport", "stdio",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    # ── MCP JSON-RPC over stdio (newline-delimited) ──────────────────────────
    async def _send(self, obj: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _read_result(self, expect_id: int) -> dict[str, Any]:
        assert self._proc and self._proc.stdout
        while True:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self.request_timeout_s)
            if not line:
                raise RuntimeError("graph server closed the stream")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == expect_id:
                if "error" in msg:
                    raise RuntimeError(f"graph server error: {msg['error']}")
                return msg.get("result", {})

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._rpc_id += 1
        rid = self._rpc_id
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return await self._read_result(rid)

    async def _handshake(self) -> None:
        await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "forge-warden", "version": "0.2"},
        })
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = (await self._request("tools/list", {})).get("tools", [])
        self.tool_names = [t.get("name", "") for t in tools]

    # ── The surface the graph tool uses ──────────────────────────────────────
    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Call one Graphify MCP tool and return its text content. Serialized so
        concurrent graph queries can't interleave frames on the one stdio pipe."""
        if not self.available:
            raise RuntimeError(f"graph unavailable ({self.unavailable_reason})")
        async with self._lock:
            result = await self._request("tools/call", {"name": name, "arguments": arguments})
        blocks = result.get("content", [])
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if result.get("isError"):
            raise RuntimeError(text or "graph tool reported an error")
        return text

    async def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
