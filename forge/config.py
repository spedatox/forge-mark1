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
    # LLM — multi-provider, routed by the profile's "provider:model" ref
    # (bare name → Anthropic). Mirrors Mark VI's llm_client.
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    zai_api_key: str = ""
    deepseek_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434/v1"
    # Opt-in provider redundancy: comma-separated "provider:model" refs tried in
    # order when opening the primary stream fails. Empty = off (fail loud).
    llm_fallback_chain: str = ""

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
            openai_api_key=_env("OPENAI_API_KEY"),
            gemini_api_key=_env("GEMINI_API_KEY"),
            zai_api_key=_env("ZAI_API_KEY"),
            deepseek_api_key=_env("DEEPSEEK_API_KEY"),
            ollama_base_url=_env("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            llm_fallback_chain=_env("FORGE_LLM_FALLBACK_CHAIN"),
            speda_api_key=_env("SPEDA_API_KEY"),
            speda_ws_url=_env("SPEDA_WS_URL", "ws://127.0.0.1:8000/agents/ws/optimus"),
            cell_backend=_env("FORGE_CELL_BACKEND", "auto") or "auto",
            workspace_root=Path(_env("FORGE_WORKSPACE_ROOT", "./.forge/workspaces")).resolve(),
            cell_image=_env("FORGE_CELL_IMAGE", "python:3.12-slim"),
            graphify_bin=_env("FORGE_GRAPHIFY_BIN"),
        )
