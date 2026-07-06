"""Environment-driven settings for the Forge process.

Deliberately flat and small. There is one environment and one operator (§3,
§9.5): no feature flags, no multi-source config layering, no profiles-of-
profiles. Values that are required-when-used are validated at their point of
use and fail loud, not here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class ForgeSettings:
    # LLM
    anthropic_api_key: str = ""

    # Mark VI link
    speda_api_key: str = ""
    speda_ws_url: str = "ws://127.0.0.1:8000/agents/ws/optimus"

    # Cell
    cell_backend: str = "auto"          # docker | subprocess | auto
    workspace_root: Path = field(default_factory=lambda: Path("./.forge/workspaces"))
    cell_image: str = "python:3.12-slim"

    # Graphify
    graphify_bin: str = ""              # blank → resolve "graphify" from PATH

    @classmethod
    def from_env(cls) -> "ForgeSettings":
        return cls(
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            speda_api_key=_env("SPEDA_API_KEY"),
            speda_ws_url=_env("SPEDA_WS_URL", "ws://127.0.0.1:8000/agents/ws/optimus"),
            cell_backend=_env("FORGE_CELL_BACKEND", "auto") or "auto",
            workspace_root=Path(_env("FORGE_WORKSPACE_ROOT", "./.forge/workspaces")).resolve(),
            cell_image=_env("FORGE_CELL_IMAGE", "python:3.12-slim"),
            graphify_bin=_env("FORGE_GRAPHIFY_BIN"),
        )
