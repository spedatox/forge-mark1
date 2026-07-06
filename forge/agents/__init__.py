"""Agent configuration — the rebrandable-core boundary (§2).

An agent is not code in the engine; it is a folder here holding two files, exactly
mirroring how Mark VI forks an agent (its `profiles/<id>.py` + `prompts/agents/<id>/`):

    forge/agents/<id>/profile.toml      identity, model policy, tool allowlist,
                                        Cell policy   (≈ Mark VI AgentProfile)
    forge/agents/<id>/system_prompt.md  the system prompt  (≈ Mark VI prompt dir)

The Warden engine imports nothing from here and contains no agent names. A third
privileged agent is added by writing a new folder — never by editing the engine.
"""
from forge.agents.config import AgentConfig
from forge.agents.registry import AgentRegistry

__all__ = ["AgentConfig", "AgentRegistry"]
