"""AgentConfig — the injected identity the Warden is parameterized with (§2).

Structurally no different from Mark VI's fork contract: the engine is untouched by
identity; identity is data loaded from the agent's folder.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CellSpec:
    allow_network: bool = False     # default posture: no outbound network (§8)
    cpus: float = 1.0
    memory_mb: int = 1024
    timeout_s: int = 60
    backend: str | None = None      # None → use the process-wide FORGE_CELL_BACKEND
    image: str | None = None        # None → use the process-wide FORGE_CELL_IMAGE.
    #                                 A security agent points this at a toolchain
    #                                 image (nmap/nikto/…) so its Cell IS its lab.
    # ── Lab posture (operator-set, mirrors CellPolicy) ──────────────────────────
    # Off by default — every agent's Cell is non-root with ALL caps dropped. A
    # security agent that must provision its own tooling and run raw-socket scans
    # opts in here; nothing a job says can reach these.
    run_as_root: bool = False       # container uid 0 (apt, privileged tooling)
    cap_add: tuple[str, ...] = ()   # Linux caps whitelisted back (e.g. NET_RAW)


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str                   # the discriminator; unique, stable (§2, mirrors Mark VI)
    name: str
    domain: str
    model_ref: str                  # model IDs live in the profile, never in core (Rule 10)
    tool_names: tuple[str, ...]     # the tool allowlist — the security boundary (§2/§4)
    system_prompt: str
    permission_mode: str = "act"    # "act" | "plan" (§6)
    max_iterations: int = 30        # the single iteration ceiling (§3)
    cell: CellSpec = CellSpec()
