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


# ── §11 peer shuts down on request while a healthy connection is idle ────────
class _IdleSocket:
    """A connected socket that never delivers a frame — the steady state."""

    def __init__(self) -> None:
        self.closed = False

    async def send(self, _payload):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()        # parks forever, like ws.recv()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.closed = True
        return False


def test_stop_request_ends_an_idle_connection(monkeypatch):
    """A stop must be observed while connected, not only between reconnects.

    Regression: run_forever checked the stop event only around _serve_one, so a
    healthy idle socket kept the peer parked in ws.recv() and SIGTERM did
    nothing until systemd escalated to SIGKILL 90s later.
    """
    import sys
    import types

    from forge.agents.registry import AgentRegistry
    from forge.config import ForgeSettings
    from forge.gate.peer import ForgePeer

    sock = _IdleSocket()
    fake = types.ModuleType("websockets")
    fake.__version__ = "13.0"
    fake.connect = lambda *_a, **_kw: sock
    monkeypatch.setitem(sys.modules, "websockets", fake)

    monkeypatch.setenv("SPEDA_API_KEY", "test-key")
    registry = AgentRegistry.load()
    peer = ForgePeer(registry.get("optimus"), ForgeSettings.from_env(), registry)

    async def scenario():
        runner = asyncio.create_task(peer.run_forever())
        await asyncio.sleep(0.05)           # let it connect and register
        peer.request_stop()                 # what the SIGTERM handler calls
        await asyncio.wait_for(runner, timeout=2.0)

    asyncio.run(scenario())                 # TimeoutError here = the bug is back
    assert sock.closed, "the socket should be closed on the way out"
