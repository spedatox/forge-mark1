"""Offline end-to-end demo (§10 'what done looks like').

Proves, with no API key and no network:
  * the Gate accepts a well-formed job (and rejects a malformed one — see tests),
  * Optimus's Warden runs a real act→observe→adapt loop against a trivial task
    using a real Cell (the SubprocessCell when Docker is absent),
  * Graphify is actually queried during the loop (graph_overview + graph_query),
  * results stream — intermediate tool/tool_result events print before the final.

The task: a tiny repo with a buggy `add()` and a failing check. Optimus maps the
codebase via the graph, runs the check (observes the failure), reads the file,
fixes it, re-runs the check (observes the pass), and finishes. The model is the
deterministic ScriptedModel whose steps branch on the running transcript — a real
observe/adapt, not a fixed tape.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from forge.agents.registry import AgentRegistry
from forge.config import ForgeSettings
from forge.gate.protocol import JobEvent, JobRequest
from forge.gate.runner import run_job
from forge.model.scripted import ScriptedModel, tool_call

BUGGY_CALC = """\
def add(a, b):
    # BUG: subtracts instead of adds
    return a - b


def scale(x, factor):
    return add(x, x) * factor
"""

CHECK = """\
from calc import add

result = add(2, 3)
assert result == 5, f"add(2,3) should be 5 but was {result}"
print("CHECK OK")
"""


def _last_tool_text(messages: list[dict]) -> str:
    """Most recent tool_result text — how a step 'observes' the prior action."""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return str(block.get("content", ""))
    return ""


def _build_script() -> ScriptedModel:
    def step_orient(_m):
        return ("First, let me map this codebase using the knowledge graph.",
                [tool_call("graph_overview", top_n=5)])

    def step_locate(_m):
        return ("Now I'll ask the graph where `add` lives and what depends on it.",
                [tool_call("graph_query", question="add function definition and callers")])

    def step_run_first(_m):
        return ("Let me run the check to see the current behavior.",
                [tool_call("run_command", command="python check.py")])

    def step_read(messages):
        # Observe: the check should have failed. Adapt by reading the source.
        observed = _last_tool_text(messages)
        note = "The check failed as expected — reading calc.py to find the bug." \
            if "OK" not in observed else "Unexpected pass; reading anyway."
        return (note, [tool_call("read_file", path="calc.py")])

    def step_fix(_m):
        return ("Found it: `add` subtracts. Fixing the operator.",
                [tool_call("edit_file", path="calc.py",
                           old_string="return a - b", new_string="return a + b")])

    def step_verify(_m):
        return ("Re-running the check to confirm the fix.",
                [tool_call("run_command", command="python check.py")])

    def step_done(messages):
        observed = _last_tool_text(messages)
        if "CHECK OK" in observed:
            return ("Done. The bug in `add` was a subtraction instead of an addition; "
                    "I fixed it and the check now passes.", [])
        # Adapt: if it still fails, surface that instead of claiming success.
        return (f"The check still fails after the edit. Observed:\n{observed}", [])

    return ScriptedModel([step_orient, step_locate, step_run_first,
                          step_read, step_fix, step_verify, step_done])


async def run_demo() -> int:
    import sys
    try:  # Windows consoles default to a legacy codepage; force UTF-8 for output.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    repo = Path(tempfile.mkdtemp(prefix="forge-demo-"))
    (repo / "calc.py").write_text(BUGGY_CALC, encoding="utf-8")
    (repo / "check.py").write_text(CHECK, encoding="utf-8")

    settings = ForgeSettings.from_env()
    registry = AgentRegistry.load()
    request = JobRequest(
        agent="optimus",
        task="There's a failing check in this repo. Find the bug, fix it, and verify.",
        repo_path=str(repo),
    )

    print(f"\n=== FORGE DEMO ===\nrepo: {repo}\ncell backend: {settings.cell_backend} "
          f"(auto → subprocess when Docker is absent)\n")

    async def emit(ev: JobEvent) -> None:
        # This is the streaming surface — events print AS THEY HAPPEN (§10).
        if ev.type == "chunk":
            print(str(ev.data), end="", flush=True)
        elif ev.type == "tool":
            print(f"\n  ┌─ tool → {ev.data['name']}({ev.data['input']})", flush=True)
        elif ev.type == "tool_result":
            d = ev.data
            preview = str(d["content"]).replace("\n", "\n  │  ")[:300]
            flag = " [is_error]" if d["is_error"] else ""
            print(f"\n  └─ result{flag}: {preview}\n", flush=True)
        elif ev.type == "started":
            print(f"[started {ev.data}]\n", flush=True)
        elif ev.type in ("done", "error"):
            print(f"\n[{ev.type}]", flush=True)

    terminal = await run_job(request, settings=settings, registry=registry,
                             emit=emit, model=_build_script())

    print(f"\n\n=== TERMINAL ===\nreason: {terminal.reason.value}\n"
          f"iterations: {terminal.iterations}\nfinal: {terminal.final_text}")
    # Prove the fix actually landed on disk in the Cell workspace.
    fixed = (repo / "calc.py").read_text(encoding="utf-8")
    ok = "return a + b" in fixed and terminal.reason.value == "completed"
    print(f"\nfix present on disk: {'return a + b' in fixed}   loop completed: "
          f"{terminal.reason.value == 'completed'}")
    shutil.rmtree(repo, ignore_errors=True)
    print("=== DEMO PASSED ===" if ok else "=== DEMO FAILED ===")
    return 0 if ok else 1
