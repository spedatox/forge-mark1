"""Cell factory — picks a backend from config and builds a fresh, unshared Cell
for one agent (§9.1). The Warden never constructs a Cell directly; the Gate asks
the factory per job."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from forge.cell.base import Cell, CellPolicy
from forge.cell.docker_cell import DockerCell
from forge.cell.subprocess_cell import SubprocessCell

logger = logging.getLogger("forge.cell")

CellBuilder = Callable[..., Cell]

# Seam 6. `auto` resolution stays core policy — which isolation to prefer when
# the operator has not said is a judgement about safety, not an extension point.
_BACKENDS: dict[str, CellBuilder] = {}


def register_backend(name: str, builder: CellBuilder) -> None:
    """Register a Cell backend.

    A backend must implement the whole `Cell` contract or fail here rather than
    at runtime: a job that discovers halfway through that its sandbox cannot
    read a file has already done work it cannot account for."""
    missing = [m for m in ("start", "run", "write", "read", "reset", "close")
               if not callable(getattr(builder, m, None)) and not hasattr(builder, m)]
    if missing:
        raise TypeError(f"cell backend {name!r} does not implement: {', '.join(missing)}")
    _BACKENDS[name] = builder


register_backend("docker", DockerCell)
register_backend("subprocess", SubprocessCell)


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

    builder = _BACKENDS.get(choice)
    if builder is None:
        known = ", ".join(sorted(_BACKENDS)) or "(none)"
        raise ValueError(f"unknown cell backend: {backend!r}. Registered: {known}.")

    if choice == "docker":
        cell: Cell = builder(name_hint=agent_id, image=image, policy=policy,
                             workspace_mount=ws)
    elif choice == "subprocess":
        cell = builder(workspace=ws, policy=policy)
    else:
        # A registered third-party backend gets everything and takes what it
        # needs; the two builtins keep their existing signatures rather than
        # being reshaped to fit a contract nothing else uses yet.
        cell = builder(agent_id=agent_id, workspace=ws, policy=policy, image=image)

    await cell.start()
    return cell
