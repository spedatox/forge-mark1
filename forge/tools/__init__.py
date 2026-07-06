"""The Forge's curated tool set.

A small, fixed toolset, so every schema is sent on turn 1 — no deferred-loading /
ToolSearch machinery (study §2 open question 5: only needed when the tool list
strains the prompt, which a curated set does not). Each tool declares its own
harness-side safety flags; the loop and permission engine read those, the model
never sees them.
"""
from forge.tools.shell import RunCommand
from forge.tools.files import ReadFile, WriteFile, EditFile
from forge.tools.graph import GraphQuery, GraphPath, GraphOverview

# Reusable tool groups, referenced by agent configs via their allowlist (§2).
CODING_TOOLS = [ReadFile, WriteFile, EditFile, RunCommand, GraphQuery, GraphPath, GraphOverview]

ALL_TOOLS = {cls.name: cls for cls in [
    RunCommand, ReadFile, WriteFile, EditFile, GraphQuery, GraphPath, GraphOverview,
]}

__all__ = ["ALL_TOOLS", "CODING_TOOLS", "RunCommand", "ReadFile", "WriteFile",
           "EditFile", "GraphQuery", "GraphPath", "GraphOverview"]
