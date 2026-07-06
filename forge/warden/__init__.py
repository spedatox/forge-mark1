"""The Warden — the parameterized loop engine (§2, §3).

One while-true loop, one mutable state object, one typed exit. The engine holds
ZERO identity strings: an agent is injected as an AgentConfig (identity, system
prompt, tool allowlist, model policy, Cell policy). Every privileged agent is the
same engine with a different config (§9.4); adding one is a new config, never an
engine edit (§2). The concrete agents live under forge/agents/, not here.
"""
from forge.warden.state import LoopState, StopReason, Terminal
from forge.warden.tool import Tool, ToolContext, ToolResult
from forge.warden.engine import Warden

__all__ = ["Warden", "LoopState", "StopReason", "Terminal",
           "Tool", "ToolContext", "ToolResult"]
