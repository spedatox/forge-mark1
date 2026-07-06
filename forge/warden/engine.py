"""The Warden loop engine (§3).

One `while True`. One mutable `LoopState`. One typed `Terminal`. The sole stop
condition is 'the model stopped requesting tools'. The interrupt signal is checked
at both boundaries — after the model responds and after tools execute — and yields
a clean, well-formed stop. Tools run concurrently only when every tool in the batch
declares itself concurrency-safe (§4 fail-closed).

The engine contains no identity strings (§2): system prompt, tool set, model, and
Cell all arrive as parameters. It is the same engine for every configured agent;
a new agent is added purely as configuration, never by editing this file.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from forge.model.base import Model, TextDelta, ToolUseRequest
from forge.warden.dispatch import dispatch_tool, to_anthropic_tool_result
from forge.warden.state import LoopState, StopReason, Terminal
from forge.warden.tool import Tool, ToolContext, ToolResult

logger = logging.getLogger("forge.warden")

Emit = Callable[[dict[str, Any]], Awaitable[None]]


async def _noop_emit(_event: dict[str, Any]) -> None:
    return None


class Warden:
    """The parameterized execution loop. Instantiated per job with everything it
    needs injected; holds no agent identity of its own."""

    def __init__(
        self,
        *,
        system_prompt: str,
        tools: dict[str, Tool],
        model: Model,
        ctx: ToolContext,
        max_iterations: int = 30,
        signal: asyncio.Event | None = None,
        emit: Emit | None = None,
    ) -> None:
        self.system_prompt = system_prompt
        self.tools = tools
        self.model = model
        self.ctx = ctx
        self.max_iterations = max_iterations
        self.signal = signal or asyncio.Event()
        self.emit = emit or _noop_emit

    async def run(self, task: str) -> Terminal:
        """Drive the loop for one job and return its single typed Terminal."""
        state = LoopState(messages=[{"role": "user", "content": task}])
        tool_schemas = [t.schema() for t in self.tools.values()]

        while True:
            # ── Budget boundary: the single iteration ceiling (§3). ──────────
            if state.iteration >= self.max_iterations:
                await self.emit({"type": "error",
                                 "data": f"reached max_iterations ({self.max_iterations})"})
                return self._terminal(StopReason.MAX_ITERATIONS, state,
                                      error=f"max_iterations ({self.max_iterations}) reached")
            state.iteration += 1

            # ── Act: stream one model turn, collecting text + tool-use blocks. ─
            assistant_content: list[dict[str, Any]] = []
            tool_uses: list[ToolUseRequest] = []
            text_buf: list[str] = []
            try:
                async for ev in self.model.stream(
                    system=self.system_prompt,
                    messages=state.messages,
                    tools=tool_schemas,
                    signal=self.signal,
                ):
                    if isinstance(ev, TextDelta):
                        text_buf.append(ev.text)
                        await self.emit({"type": "chunk", "data": ev.text})
                    elif isinstance(ev, ToolUseRequest):
                        tool_uses.append(ev)
                        await self.emit({"type": "tool",
                                         "data": {"id": ev.id, "name": ev.name, "input": ev.input}})
            except Exception as e:  # noqa: BLE001 — surface loudly, don't degrade (§9.5)
                logger.exception("model_stream_failed")
                await self.emit({"type": "error", "data": f"model error: {e}"})
                return self._terminal(StopReason.ERROR, state, error=f"{type(e).__name__}: {e}")

            text = "".join(text_buf)
            if text:
                assistant_content.append({"type": "text", "text": text})
                state.last_text = text
            for tu in tool_uses:
                assistant_content.append(
                    {"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input})
            state.messages.append({"role": "assistant",
                                   "content": assistant_content or [{"type": "text", "text": ""}]})

            # ── Boundary 1: interrupt checked after the model responds (§3). ──
            if self.signal.is_set():
                # Back-fill tool_results so the transcript stays well-formed even
                # though we did not run the tools (study §4).
                if tool_uses:
                    state.messages.append(self._interrupted_results(tool_uses))
                return self._aborted(state)

            # ── Stop condition: no tool-use blocks → the model is done (§3). ──
            if not tool_uses:
                await self.emit({"type": "done", "data": text})
                return self._terminal(StopReason.COMPLETED, state)

            # ── Observe: run the requested tools (parallel only where safe). ──
            results = await self._run_tools(tool_uses)
            result_blocks = [to_anthropic_tool_result(tu.id, res)
                             for tu, res in zip(tool_uses, results)]
            for tu, res in zip(tool_uses, results):
                await self.emit({"type": "tool_result",
                                 "data": {"tool_use_id": tu.id, "is_error": res.is_error,
                                          "content": res.content}})
            state.messages.append({"role": "user", "content": result_blocks})

            # ── Boundary 2: interrupt checked after tools execute (§3). ───────
            if self.signal.is_set():
                return self._aborted(state)
            # loop

    # ── Tool execution: concurrency gated by declared safety (§4). ───────────
    async def _run_tools(self, tool_uses: list[ToolUseRequest]) -> list[ToolResult]:
        results: dict[str, ToolResult] = {}

        def is_safe(tu: ToolUseRequest) -> bool:
            tool = self.tools.get(tu.name)
            return bool(tool and tool.is_concurrency_safe)

        safe = [tu for tu in tool_uses if is_safe(tu)]
        unsafe = [tu for tu in tool_uses if not is_safe(tu)]

        # Read-only / concurrency-safe tools run together.
        if safe:
            done = await asyncio.gather(
                *(dispatch_tool(self.tools, tu.name, tu.input, self.ctx) for tu in safe))
            for tu, res in zip(safe, done):
                results[tu.id] = res
        # Everything else runs sequentially to avoid clobbering shared Cell state.
        for tu in unsafe:
            results[tu.id] = await dispatch_tool(self.tools, tu.name, tu.input, self.ctx)

        return [results[tu.id] for tu in tool_uses]

    # ── Terminal helpers ─────────────────────────────────────────────────────
    def _interrupted_results(self, tool_uses: list[ToolUseRequest]) -> dict[str, Any]:
        blocks = [to_anthropic_tool_result(
            tu.id, ToolResult("[interrupted before execution]", is_error=True))
            for tu in tool_uses]
        return {"role": "user", "content": blocks}

    def _aborted(self, state: LoopState) -> Terminal:
        state.messages.append(
            {"role": "user", "content": "[the operator interrupted this run]"})
        return self._terminal(StopReason.ABORTED, state)

    def _terminal(self, reason: StopReason, state: LoopState, error: str | None = None) -> Terminal:
        return Terminal(reason=reason, final_text=state.last_text,
                        iterations=state.iteration, error=error, messages=state.messages)
