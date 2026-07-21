"""Assembling the extension layer — the one place that reads config from disk.

Law 2 of MARK2_SEAMS says all seam wiring happens where jobs are assembled, and
nothing self-registers via import side effects. This is that place for
operator-supplied extensions: it reads a config file, builds providers and
fragments, and hands them to `run_job`. A future plugin loader is one more caller
of the same functions, not a different mechanism.

The config is one JSON file, `.forge/extensions.json`:

    {
      "mcpServers": {
        "graphite": {"command": "npx", "args": ["-y", "@acme/graphite-mcp"],
                     "env": {"GRAPHITE_TOKEN": "..."}}
      },
      "skillsDirs": ["./.forge/skills"]
    }

Absent file means no extensions, which is the default posture and not an error.
Every failure here degrades to "that extension is not available" and is logged:
an operator's broken MCP config must not be able to stop an execution peer from
doing the work it was dispatched to do.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from forge.agents.prompt import PromptFragment
from forge.mcp.client import MCPServerSpec
from forge.mcp.provider import MCPToolProvider
from forge.skills.provider import SkillProvider
from forge.warden.toolsource import BuiltinToolProvider, ToolProvider

logger = logging.getLogger("forge.extensions")

DEFAULT_CONFIG = Path("./.forge/extensions.json")
DEFAULT_SKILLS_DIR = Path("./.forge/skills")


@dataclass
class Extensions:
    """What the operator's configuration contributed."""
    providers: list[ToolProvider] = field(default_factory=list)
    fragments: list[PromptFragment] = field(default_factory=list)
    hooks: list = field(default_factory=list)

    def tool_providers(self) -> list[ToolProvider]:
        """Builtins first, so an extension can never shadow a core tool — the
        fold refuses collisions, and refusing means the *later* source loses."""
        return [BuiltinToolProvider(), *self.providers]


def _read_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("extensions_config_unreadable",
                       extra={"path": str(path), "error": repr(e)})
        return {}
    return data if isinstance(data, dict) else {}


def _server_specs(config: dict) -> list[MCPServerSpec]:
    specs: list[MCPServerSpec] = []
    for name, entry in (config.get("mcpServers") or {}).items():
        if not isinstance(entry, dict) or not entry.get("command"):
            logger.warning("mcp_server_config_invalid", extra={"server": name})
            continue
        specs.append(MCPServerSpec(
            name=name,
            command=str(entry["command"]),
            args=tuple(str(a) for a in entry.get("args") or ()),
            env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
        ))
    return specs


def load_extensions(config_path: Path = DEFAULT_CONFIG,
                    skills_dir: Path = DEFAULT_SKILLS_DIR) -> Extensions:
    """Build the extension layer from disk. Never raises."""
    config = _read_config(config_path)
    ext = Extensions()

    roots = [Path(p) for p in (config.get("skillsDirs") or [])] or [skills_dir]
    skills = SkillProvider.from_dirs(*roots)
    if skills.skills:
        ext.providers.append(skills)
        fragment = skills.fragment()
        if fragment is not None:
            ext.fragments.append(fragment)
        logger.info("skills_loaded", extra={"count": len(skills.skills)})

    for spec in _server_specs(config):
        ext.providers.append(MCPToolProvider(spec))

    return ext
