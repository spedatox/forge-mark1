"""run_job — the single assembly point behind every Gate front door.

Given a validated JobRequest and an agent config, it builds a FRESH, UNSHARED Cell
(§9.1), starts a Graphify sidecar over the job's repo (§5), constructs the model
and tools from the agent's identity, runs the Warden loop, and streams JobEvents
as they happen. It always tears the Cell and sidecar down, even on failure.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

from forge.agents.config import AgentConfig
from forge.agents.registry import AgentRegistry
from forge.cell.base import CellPolicy
from forge.cell.factory import build_cell
from forge.config import ForgeSettings
from forge.gate.protocol import JobEvent, JobRequest
from forge.graph.sidecar import GraphSidecar
from forge.model.base import Model
from forge.tools import ALL_TOOLS
from forge.warden.engine import Warden
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import AllowList, Mode, PermissionEngine
from forge.warden.state import StopReason, Terminal
from forge.warden.tool import ToolContext

logger = logging.getLogger("forge.gate")

EmitEvent = Callable[[JobEvent], Awaitable[None]]


async def run_job(
    request: JobRequest,
    *,
    settings: ForgeSettings,
    registry: AgentRegistry,
    emit: EmitEvent,
    model: Model | None = None,
    signal: asyncio.Event | None = None,
    allowlist: AllowList | None = None,
) -> Terminal:
    """Run one job to a single Terminal, streaming JobEvents via `emit`.

    `model` may be injected (the demo/tests pass a ScriptedModel); when None, the
    real model is built from the agent profile's model_ref."""
    cfg = registry.get(request.agent)
    signal = signal or asyncio.Event()

    async def out(etype: str, data=None) -> None:
        await emit(JobEvent(job_id=request.job_id, type=etype, data=data))

    await out("started", {"agent": cfg.agent_id, "job_id": request.job_id})

    # Resolve constraints over profile defaults (§7 overrides profile).
    c = request.constraints
    max_iterations = c.max_iterations or cfg.max_iterations
    allow_network = c.network or cfg.cell.allow_network
    repo_path = Path(request.repo_path).resolve() if request.repo_path else None

    policy = CellPolicy(
        allow_network=allow_network,
        cpus=cfg.cell.cpus,
        memory_mb=cfg.cell.memory_mb,
        default_timeout_s=c.timeout_s or cfg.cell.timeout_s,
    )

    cell = None
    graph = None
    try:
        cell = await build_cell(
            agent_id=cfg.agent_id,
            workspace_root=settings.workspace_root,
            backend=cfg.cell.backend or settings.cell_backend,
            image=settings.cell_image,
            policy=policy,
            workspace=repo_path,
        )

        # Graphify sidecar over the job's repo — Warden-side, indexed once (§5).
        if repo_path is not None:
            graph = GraphSidecar(repo_path)
            await graph.start()
            await out("chunk", f"[graph: {'ready' if graph.available else 'unavailable'}]\n")

        # Heartbreaker's model picker overrides the profile's default for this
        # job; None means "use the profile's model_ref" (Rule 10: model IDs
        # live only in profiles, and the override is a profile-level concept).
        model_ref = request.model_override or cfg.model_ref
        model = model or _build_model(model_ref, settings)

        tools = {name: ALL_TOOLS[name]() for name in cfg.tool_names}
        ctx = ToolContext(
            agent_id=cfg.agent_id,
            cell=cell,
            graph=graph,
            files=FileStateCache(),
            permissions=PermissionEngine(
                mode=Mode(cfg.permission_mode),
                allowlist=allowlist or AllowList(),
            ),
            network_allowed=allow_network,
        )

        warden = Warden(
            system_prompt=cfg.system_prompt,
            tools=tools,
            model=model,
            ctx=ctx,
            max_iterations=max_iterations,
            signal=signal,
            emit=lambda ev: emit(JobEvent(job_id=request.job_id, type=ev["type"], data=ev.get("data"))),
        )
        terminal = await warden.run(request.task)
        return terminal
    except Exception as e:  # noqa: BLE001 — fail loud (§9.5) as a terminal error event
        logger.exception("run_job_failed")
        await out("error", f"{type(e).__name__}: {e}")
        return Terminal(reason=StopReason.ERROR, error=f"{type(e).__name__}: {e}")
    finally:
        if graph is not None:
            await graph.close()
        if cell is not None:
            await cell.close()


def _build_model(model_ref: str, settings: ForgeSettings) -> Model:
    """Construct the model from a ``provider:model`` ref via the multi-provider
    factory (Anthropic / OpenAI / Gemini / z.ai / DeepSeek / Ollama).  The ref
    is either the agent profile's default or a per-job override from Heartbreaker's
    model picker.  A missing key for the selected provider fails loud."""
    from forge.model.factory import build_model
    return build_model(model_ref, settings)
