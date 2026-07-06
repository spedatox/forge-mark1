"""Tests for the load-bearing patterns (§3, §4, §6, §2). Async cases wrap
asyncio.run so the suite needs no pytest-asyncio plugin."""
import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from forge.gate.protocol import JobRequest
from forge.model.scripted import ScriptedModel, tool_call
from forge.warden.engine import Warden
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import AllowList, Mode, PermissionEngine
from forge.warden.state import StopReason
from forge.warden.tool import Tool, ToolContext, ToolResult

ENGINE_ROOT = Path(__file__).resolve().parent.parent / "forge"


# ── Test doubles ─────────────────────────────────────────────────────────────
class EchoArgs(BaseModel):
    text: str


class Echo(Tool):
    name = "echo"
    description = "echo the input text"
    Args = EchoArgs
    is_read_only = True
    is_concurrency_safe = True

    async def call(self, args: EchoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult(args.text)


def _ctx() -> ToolContext:
    return ToolContext(agent_id="t", cell=None, graph=None, files=FileStateCache(),
                       permissions=PermissionEngine(), network_allowed=False)


def _warden(steps, tools=None, max_iter=30, signal=None) -> Warden:
    return Warden(system_prompt="", tools=tools or {"echo": Echo()},
                  model=ScriptedModel(steps), ctx=_ctx(), max_iterations=max_iter,
                  signal=signal)


# ── §3 the loop ──────────────────────────────────────────────────────────────
def test_stop_condition_is_no_tool_calls():
    """The sole stop signal: a turn with no tool_use → completed."""
    steps = [lambda m: ("using a tool", [tool_call("echo", text="hi")]),
             lambda m: ("all done", [])]
    term = asyncio.run(_warden(steps).run("go"))
    assert term.reason is StopReason.COMPLETED
    assert term.final_text == "all done"


def test_max_iterations_guard():
    """A model that never stops hits the single ceiling."""
    steps = [lambda m: ("again", [tool_call("echo", text="x")])] * 50
    term = asyncio.run(_warden(steps, max_iter=3).run("go"))
    assert term.reason is StopReason.MAX_ITERATIONS
    assert term.iterations == 3


def test_interrupt_yields_clean_aborted():
    sig = asyncio.Event()
    sig.set()
    steps = [lambda m: ("x", [tool_call("echo", text="x")])]
    term = asyncio.run(_warden(steps, signal=sig).run("go"))
    assert term.reason is StopReason.ABORTED


# ── §4 the tool boundary: errors-as-results, never exceptions ────────────────
def test_unknown_tool_becomes_error_result_not_crash():
    steps = [lambda m: ("call missing", [tool_call("does_not_exist", x=1)]),
             lambda m: ("done", [])]
    term = asyncio.run(_warden(steps).run("go"))
    assert term.reason is StopReason.COMPLETED  # loop survived the bad call
    blocks = [b for msg in term.messages if isinstance(msg.get("content"), list)
              for b in msg["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert any(b.get("is_error") for b in blocks)


def test_bad_input_becomes_error_result():
    steps = [lambda m: ("bad args", [tool_call("echo")]),  # missing required 'text'
             lambda m: ("done", [])]
    term = asyncio.run(_warden(steps).run("go"))
    assert term.reason is StopReason.COMPLETED
    blocks = [b for msg in term.messages if isinstance(msg.get("content"), list)
              for b in msg["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert any(b.get("is_error") and "Invalid input" in b.get("content", "") for b in blocks)


# ── §6 permission & safety gate ──────────────────────────────────────────────
def test_safety_gate_blocks_protected_paths():
    eng = PermissionEngine(mode=Mode.ACT)
    from forge.tools.files import WriteFile
    d = eng.resolve(WriteFile(), {"path": ".git/config", "content": "x"}, None)
    assert not d.allowed and "safety gate" in d.reason


def test_safety_gate_blocks_destructive_command():
    eng = PermissionEngine(mode=Mode.ACT)
    from forge.tools.shell import RunCommand
    d = eng.resolve(RunCommand(), {"command": "rm -rf /"}, None)
    assert not d.allowed


def test_gate_is_bypass_immune_even_with_allowlist():
    """An allow-list hit can never override the gate (§6)."""
    al = AllowList({"run_command"})  # operator allow-listed the whole tool
    eng = PermissionEngine(mode=Mode.ACT, allowlist=al)
    from forge.tools.shell import RunCommand
    d = eng.resolve(RunCommand(), {"command": "git push --force origin main"}, None)
    assert not d.allowed


def test_plan_mode_denies_mutations_allows_reads():
    eng = PermissionEngine(mode=Mode.PLAN)
    from forge.tools.files import WriteFile, ReadFile
    assert not eng.resolve(WriteFile(), {"path": "a.txt", "content": "x"}, None).allowed
    assert eng.resolve(ReadFile(), {"path": "a.txt"}, None).allowed


def test_allowlist_lets_normal_command_through():
    eng = PermissionEngine(mode=Mode.ACT, allowlist=AllowList({"run_command:pytest*"}))
    from forge.tools.shell import RunCommand
    assert eng.resolve(RunCommand(), {"command": "pytest -q"}, None).allowed


# ── read-before-write freshness (study §3) ───────────────────────────────────
def test_read_before_write_freshness():
    fs = FileStateCache()
    assert fs.freshness_error("a.py", "hash1") is not None      # never read → blocked
    fs.record("a.py", "code", "hash1")
    assert fs.freshness_error("a.py", "hash1") is None           # read & unchanged → ok
    assert fs.freshness_error("a.py", "hash2") is not None       # changed since read → blocked


# ── §7 wire contract validation ──────────────────────────────────────────────
def test_malformed_job_request_rejected():
    with pytest.raises(Exception):
        JobRequest.model_validate_json('{"task": "no agent field"}')
    ok = JobRequest.model_validate_json('{"agent": "optimus", "task": "do it"}')
    assert ok.agent == "optimus" and ok.constraints.network is False


# ── §2 rebrandable core: no identity strings in the engine ───────────────────
def test_engine_core_has_no_agent_identity_strings():
    core = ["warden/__init__.py", "warden/engine.py", "warden/state.py",
            "warden/tool.py", "warden/dispatch.py", "warden/permissions.py",
            "warden/filestate.py"]
    for rel in core:
        text = (ENGINE_ROOT / rel).read_text(encoding="utf-8").lower()
        assert "optimus" not in text, f"'optimus' leaked into {rel}"
        assert "centurion" not in text, f"'centurion' leaked into {rel}"
