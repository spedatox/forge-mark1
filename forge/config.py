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


def _int(name: str, default: int, allow_zero: bool = False) -> int:
    """A malformed number falls back to the default rather than failing startup —
    these are gauges and ceilings, and a typo should not take the peer down.
    `allow_zero` is for settings where nought is a real choice: retries off."""
    try:
        value = int(_env(name) or default)
    except ValueError:
        return default
    return value if (value >= 0 if allow_zero else value > 0) else default


def _float(name: str, default: float) -> float:
    try:
        value = float(_env(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


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

    # Context accounting. The window is the model's; max_tokens is what one turn
    # may produce, and doubles as the reserve the ledger keeps for a compaction
    # call. Both are per-model facts with fleet-wide defaults, overridable for a
    # small local model whose window is nothing like 200 K.
    context_limit: int = 200_000
    max_tokens: int = 16_384

    # Consecutive re-attempts at one failed model turn, and the first backoff.
    retry_attempts: int = 4
    retry_base_delay_s: float = 2.0

    # Trust. An unanswered ask denies, so the timeout is how long a gated
    # operation waits for a human before the job carries on without it.
    ask_timeout_s: float = 120.0
    allowlist_path: Path = field(default_factory=lambda: Path("./.forge/allowlist.json"))

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
            context_limit=_int("FORGE_CONTEXT_LIMIT", 200_000),
            max_tokens=_int("FORGE_MAX_TOKENS", 16_384),
            retry_attempts=_int("FORGE_RETRY_ATTEMPTS", 4, allow_zero=True),
            retry_base_delay_s=_float("FORGE_RETRY_BASE_DELAY_S", 2.0),
            ask_timeout_s=_float("FORGE_ASK_TIMEOUT_S", 120.0),
            allowlist_path=Path(_env("FORGE_ALLOWLIST_PATH", "./.forge/allowlist.json")),
        )
