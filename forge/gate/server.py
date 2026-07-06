"""Standalone native-contract WebSocket server (§7).

One connection carries one job: the client sends a JobRequest as JSON, the server
validates it (malformed → a single error JobEvent, then close) and streams
JobEvents until the job ends. This is the front door for running/testing the Forge
without Mark VI, and it is what satisfies 'Gate accepts a connection and a
well-formed job request; rejects malformed ones' (§10).
"""
from __future__ import annotations

import asyncio
import logging

import websockets
from pydantic import ValidationError

from forge.agents.registry import AgentRegistry
from forge.config import ForgeSettings
from forge.gate.protocol import JobEvent, JobRequest
from forge.gate.runner import run_job

logger = logging.getLogger("forge.gate.server")


async def serve(host: str = "127.0.0.1", port: int = 8770,
                settings: ForgeSettings | None = None,
                registry: AgentRegistry | None = None) -> None:
    settings = settings or ForgeSettings.from_env()
    registry = registry or AgentRegistry.load()

    async def handler(ws) -> None:
        try:
            raw = await ws.recv()
        except websockets.ConnectionClosed:
            return

        try:
            request = JobRequest.model_validate_json(raw)
        except ValidationError as e:
            await ws.send(JobEvent(job_id="-", type="error",
                                   data=f"malformed job request: {e}").model_dump_json())
            await ws.close(code=1003, reason="malformed job request")
            return

        async def emit(ev: JobEvent) -> None:
            try:
                await ws.send(ev.model_dump_json())
            except websockets.ConnectionClosed:
                signal.set()   # client hung up → stop the run

        signal = asyncio.Event()
        logger.info("job_accepted", extra={"agent": request.agent, "job_id": request.job_id})
        await run_job(request, settings=settings, registry=registry, emit=emit, signal=signal)
        await ws.close()

    logger.info("forge_gate_listening", extra={"host": host, "port": port})
    async with websockets.serve(handler, host, port):
        await asyncio.Future()   # run forever
