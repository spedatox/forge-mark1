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
from forge.tools.search import Grep, Glob

# Navigation — how an agent orients in a repo it did not write. Shared by every
# profile: the alternative is reading whole files, which fills the window before
# the work starts.
NAV_TOOLS = [ReadFile, Grep, Glob]

# Reusable tool groups, referenced by agent configs via their allowlist (§2).
CODING_TOOLS = [*NAV_TOOLS, WriteFile, EditFile, RunCommand,
                GraphQuery, GraphPath, GraphOverview]

# Centurion's group: run security tooling in the Cell (RunCommand) and read/write
# scan output and engagement reports (files). No graph — its subject is a live
# target's posture, not a codebase's structure. The Cell policy (allow_network)
# and the operator's authorization are the real boundary, not this list.
SECURITY_TOOLS = [*NAV_TOOLS, RunCommand, WriteFile, EditFile]

ALL_TOOLS = {cls.name: cls for cls in [
    RunCommand, ReadFile, WriteFile, EditFile, Grep, Glob,
    GraphQuery, GraphPath, GraphOverview,
]}

__all__ = ["ALL_TOOLS", "NAV_TOOLS", "CODING_TOOLS", "SECURITY_TOOLS", "RunCommand",
           "ReadFile", "WriteFile", "EditFile", "Grep", "Glob",
           "GraphQuery", "GraphPath", "GraphOverview"]
