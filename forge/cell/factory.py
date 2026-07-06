"""Cell factory — picks a backend from config and builds a fresh, unshared Cell
for one agent (§9.1). The Warden never constructs a Cell directly; the Gate asks
the factory per job."""
from __future__ import annotations

import logging
from pathlib import Path

from forge.cell.base import Cell, CellPolicy
from forge.cell.docker_cell import DockerCell
from forge.cell.subprocess_cell import SubprocessCell

logger = logging.getLogger("forge.cell")


async def build_cell(
    *,
    agent_id: str,
    workspace_root: Path,
    backend: str = "auto",
    image: str = "python:3.12-slim",
    policy: CellPolicy | None = None,
    workspace: Path | None = None,
) -> Cell:
    """Build and start one Cell for `agent_id`.

    backend: "docker" (production), "subprocess" (reduced isolation), or "auto"
    (docker if the daemon answers, else subprocess). A per-agent workspace keeps
    two Cells physically separate even under the subprocess backend. `workspace`
    overrides the default per-agent directory (used to point a Cell at the job's
    repo so shell ops and the Graphify graph see the same files).
    """
    policy = policy or CellPolicy()
    ws = Path(workspace).resolve() if workspace else (workspace_root / agent_id)
    choice = backend
    if choice == "auto":
        choice = "docker" if await DockerCell.available() else "subprocess"
        logger.info("cell_backend_auto_selected", extra={"agent_id": agent_id, "backend": choice})
    elif choice == "docker" and not await DockerCell.available():
        raise RuntimeError(
            "FORGE_CELL_BACKEND=docker but the Docker daemon is not reachable. "
            "Start Docker, or set FORGE_CELL_BACKEND=subprocess for reduced-isolation dev.")

    if choice == "docker":
        cell: Cell = DockerCell(name_hint=agent_id, image=image, policy=policy,
                                workspace_mount=ws)
    elif choice == "subprocess":
        cell = SubprocessCell(workspace=ws, policy=policy)
    else:
        raise ValueError(f"unknown cell backend: {backend!r}")

    await cell.start()
    return cell
