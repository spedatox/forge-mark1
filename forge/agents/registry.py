"""AgentRegistry — loads every agent folder into an AgentConfig (§2).

Generic and identity-free: it discovers whatever `<id>/profile.toml` +
`<id>/system_prompt.md` pairs exist under the agents directory and resolves each
profile's declared tool allowlist against the curated tool set. Adding Centurion
(or any third agent) later is purely a new folder — this loader and the engine
are untouched.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from forge.agents.config import AgentConfig, CellSpec
from forge.tools import ALL_TOOLS, CODING_TOOLS, SECURITY_TOOLS

AGENTS_DIR = Path(__file__).parent

# Named tool groups a profile may reference instead of listing every tool.
_TOOL_GROUPS = {
    "coding": tuple(cls.name for cls in CODING_TOOLS),
    "security": tuple(cls.name for cls in SECURITY_TOOLS),
}


def _resolve_tools(entries: list[str]) -> tuple[str, ...]:
    resolved: list[str] = []
    for entry in entries:
        if entry in _TOOL_GROUPS:
            resolved.extend(_TOOL_GROUPS[entry])
        elif entry in ALL_TOOLS:
            resolved.append(entry)
        else:
            raise ValueError(f"unknown tool or group in allowlist: {entry!r}")
    # de-dup, preserve order
    seen: set[str] = set()
    return tuple(t for t in resolved if not (t in seen or seen.add(t)))


class AgentRegistry:
    def __init__(self, configs: dict[str, AgentConfig]) -> None:
        self._configs = configs

    def get(self, agent_id: str) -> AgentConfig:
        try:
            return self._configs[agent_id]
        except KeyError:
            known = ", ".join(sorted(self._configs)) or "(none)"
            raise KeyError(f"no agent config for {agent_id!r}. Known agents: {known}.")

    def ids(self) -> list[str]:
        return sorted(self._configs)

    @classmethod
    def load(cls, agents_dir: Path = AGENTS_DIR) -> "AgentRegistry":
        configs: dict[str, AgentConfig] = {}
        for child in sorted(agents_dir.iterdir()):
            profile = child / "profile.toml"
            prompt = child / "system_prompt.md"
            if not (child.is_dir() and profile.exists() and prompt.exists()):
                continue
            configs[child.name] = _load_one(profile, prompt)
        if not configs:
            raise RuntimeError(f"no agent configs found under {agents_dir}")
        return cls(configs)


def _load_one(profile_path: Path, prompt_path: Path) -> AgentConfig:
    data = tomllib.loads(profile_path.read_text(encoding="utf-8"))
    cell = data.get("cell", {})
    agent_id = data["agent_id"]
    if agent_id != profile_path.parent.name:
        raise ValueError(
            f"agent_id {agent_id!r} must match folder name {profile_path.parent.name!r}")
    return AgentConfig(
        agent_id=agent_id,
        name=data["name"],
        domain=data.get("domain", ""),
        model_ref=data["model"],
        tool_names=_resolve_tools(list(data.get("tools", []))),
        system_prompt=prompt_path.read_text(encoding="utf-8"),
        permission_mode=data.get("permission_mode", "act"),
        max_iterations=int(data.get("max_iterations", 30)),
        cell=CellSpec(
            allow_network=bool(cell.get("allow_network", False)),
            cpus=float(cell.get("cpus", 1.0)),
            memory_mb=int(cell.get("memory_mb", 1024)),
            timeout_s=int(cell.get("timeout_s", 60)),
            backend=cell.get("backend"),
        ),
    )
