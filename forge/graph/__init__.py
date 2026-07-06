"""Graphify integration (§5).

Graphify (MIT, PyPI `graphifyy`) is used directly as a dependency — not
reimplemented. It is wired in as a Warden-side capability: a sidecar the Warden
talks to over MCP stdio, giving the agent a queryable knowledge graph of the
codebase so it can ask structural questions instead of re-reading whole files.
The graph is indexed once per session. This is read-side context — it never runs
inside the Cell.
"""
from forge.graph.sidecar import GraphSidecar

__all__ = ["GraphSidecar"]
