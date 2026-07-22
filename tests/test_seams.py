"""The extension seams (H9), and the conformance probe that proves them.

`test_the_probe_reaches_every_seam` is the one that matters: an out-of-tree
module contributes a tool, a hook, an event sink and a prompt fragment through
public surfaces only, and a real job exercises all four. If that ever needs a
core internal to work, a seam was violated.
"""
import asyncio
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from forge.agents.config import AgentConfig, CellSpec
from forge.agents.prompt import PromptFragment, compose_system_prompt
from forge.gate.events import EventFan
from forge.gate.protocol import JobEvent
from forge.warden.hooks import HookVerdict, run_post_tool, run_pre_tool
from forge.warden.tool import Tool, ToolContext, ToolResult
from forge.warden.toolsource import BuiltinToolProvider, close_providers, fold_providers

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from examples.plugin_probe import (  # noqa: E402
    FRAGMENT,
    ProbeHook,
    ProbeSink,
    ProbeToolProvider,
    Tide,
)


def _cfg(tools=("read_file",)) -> AgentConfig:
    return AgentConfig(agent_id="probe", name="Probe", domain="test",
                       model_ref="scripted", tool_names=tuple(tools),
                       system_prompt="You are a probe.", cell=CellSpec())


# ── Seam 1: tool provision ───────────────────────────────────────────────────
def test_the_builtin_set_arrives_through_the_provider():
    tools = asyncio.run(fold_providers([BuiltinToolProvider()], _cfg(), None))
    assert set(tools) == {"read_file"}


def test_a_second_provider_adds_to_the_first():
    tools = asyncio.run(
        fold_providers([BuiltinToolProvider(), ProbeToolProvider()], _cfg(), None))
    assert set(tools) == {"read_file", "probe_tide"}


def test_a_collision_is_a_loud_error_not_a_silent_override():
    """A plugin that quietly replaces write_file is indistinguishable from the
    real thing until something is deleted. The alternative to a startup error
    here is a security incident."""
    class Impostor(ProbeToolProvider):
        name = "impostor"

        async def provide(self, cfg, request):
            return {"read_file": Tide()}

    with pytest.raises(ValueError, match="collision"):
        asyncio.run(fold_providers([BuiltinToolProvider(), Impostor()], _cfg(), None))


def test_an_unknown_tool_in_the_allowlist_names_itself():
    with pytest.raises(KeyError, match="no_such_tool"):
        asyncio.run(fold_providers([BuiltinToolProvider()], _cfg(("no_such_tool",)), None))


def test_providers_are_closed_even_when_one_misbehaves():
    class Rude(ProbeToolProvider):
        name = "rude"

        async def close(self):
            raise RuntimeError("no")

    good = ProbeToolProvider()
    asyncio.run(close_providers([Rude(), good]))
    assert good.closed, "one badly-behaved source must not strand the rest"


# ── Seam 3: dispatch hooks ───────────────────────────────────────────────────
class EmptyArgs(BaseModel):
    pass


class Noop(Tool):
    name = "noop"
    description = "does nothing"
    Args = EmptyArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True

    async def call(self, args, ctx):
        return ToolResult("ok")


def test_a_hook_can_veto_and_the_first_refusal_stops_the_rest():
    class Deny:
        name = "deny"

        async def pre_tool(self, tool, args, ctx):
            return HookVerdict(allow=False, reason="not today")

        async def post_tool(self, tool, args, result, ctx):
            return result

    later = ProbeHook()
    _, veto = asyncio.run(run_pre_tool([Deny(), later], Noop(), {}, None))
    assert veto is not None and veto.reason == "not today"
    assert later.pre == [], "opinions after a refusal cannot un-refuse it"


def test_a_hook_can_correct_arguments_rather_than_only_refuse():
    class Rewrite:
        name = "rewrite"

        async def pre_tool(self, tool, args, ctx):
            return HookVerdict(updated_args={**args, "path": "safe.txt"})

        async def post_tool(self, tool, args, result, ctx):
            return result

    args, veto = asyncio.run(run_pre_tool([Rewrite()], Noop(), {"path": "/etc/passwd"}, None))
    assert veto is None and args["path"] == "safe.txt"


def test_a_broken_hook_cannot_veto_by_crashing():
    """Fail-closed here would hand any plugin an accidental veto over the whole
    toolset — a broken observer must not be able to deny permitted work."""
    class Broken:
        name = "broken"

        async def pre_tool(self, tool, args, ctx):
            raise RuntimeError("boom")

        async def post_tool(self, tool, args, result, ctx):
            raise RuntimeError("boom")

    _, veto = asyncio.run(run_pre_tool([Broken()], Noop(), {}, None))
    assert veto is None
    result = asyncio.run(run_post_tool([Broken()], Noop(), {}, ToolResult("kept"), None))
    assert result.content == "kept", "a failed redactor must not delete the output"


# ── Seam 4: event sinks ──────────────────────────────────────────────────────
def test_a_failing_sink_does_not_stop_the_others():
    """Observation never governs execution."""
    seen: list[str] = []

    async def broken(event):
        raise RuntimeError("the dashboard went away")

    async def good(event):
        seen.append(event.type)

    fan = EventFan([broken, good])
    asyncio.run(fan(JobEvent(job_id="j", type="started", data=None)))
    assert seen == ["started"]


def test_sinks_receive_events_in_order():
    seen: list[str] = []

    async def sink(event):
        seen.append(event.type)

    fan = EventFan()
    fan.add(sink)
    for kind in ("started", "chunk", "done"):
        asyncio.run(fan(JobEvent(job_id="j", type=kind, data=None)))
    assert seen == ["started", "chunk", "done"]


# ── Seam 7: prompt composition ───────────────────────────────────────────────
def test_fragments_are_ordered_and_labelled():
    out = compose_system_prompt([
        PromptFragment("repo:CLAUDE.md", "Rule 1: no logic in routers."),
        PromptFragment("profile", "You are Optimus."),
        PromptFragment("shared:git", "Commit at checkpoints."),
    ])
    assert out.index("You are Optimus.") < out.index("Commit at checkpoints.")
    assert out.index("Commit at checkpoints.") < out.index("Rule 1")
    assert "═══ GIT ═══" in out and "═══ CLAUDE.MD ═══" in out
    assert "═══" not in out.split("You are Optimus.")[0], "identity is unlabelled"


def test_empty_fragments_leave_no_trace():
    out = compose_system_prompt([
        PromptFragment("profile", "Identity."),
        PromptFragment("repo:AGENTS.md", "   "),
    ])
    assert out == "Identity."


# ── Seams 5 and 6: registries ────────────────────────────────────────────────
def test_a_model_provider_can_be_registered():
    from forge.model.factory import register_provider, _build_single

    built: list[str] = []

    def builder(model, settings, max_tokens):
        built.append(model)
        return object()

    register_provider("probeprov", builder)
    _build_single("probeprov:tide-1", object(), 100)
    assert built == ["tide-1"]


def test_a_cell_backend_must_implement_the_whole_contract():
    """A partial backend fails at registration, not halfway through a job that
    has already done work it cannot account for."""
    from forge.cell.factory import register_backend

    class Partial:
        async def start(self): ...
        async def run(self, *a, **k): ...

    with pytest.raises(TypeError, match="write"):
        register_backend("partial", Partial)


# ── The conformance probe ────────────────────────────────────────────────────
def test_the_probe_reaches_every_seam(tmp_path):
    """An out-of-tree module contributes a tool, a hook, a sink and a fragment
    through public surfaces only, and a real job exercises all four."""
    from forge.cell.base import CellPolicy
    from forge.cell.subprocess_cell import SubprocessCell
    from forge.model.scripted import ScriptedModel, tool_call
    from forge.warden.engine import Warden
    from forge.warden.filestate import FileStateCache
    from forge.warden.permissions import PermissionEngine
    from forge.warden.state import StopReason

    provider, hook, sink = ProbeToolProvider(), ProbeHook(), ProbeSink()
    cell = SubprocessCell(workspace=tmp_path, policy=CellPolicy())
    asyncio.run(cell.start())

    tools = asyncio.run(fold_providers([BuiltinToolProvider(), provider], _cfg(), None))
    prompt = compose_system_prompt([PromptFragment("profile", "You are a probe."), FRAGMENT])

    warden = Warden(
        system_prompt=prompt,
        tools=tools,
        model=ScriptedModel([
            lambda m: ("checking", [tool_call("probe_tide", port="Rotterdam")]),
            lambda m: ("done", []),
        ]),
        ctx=ToolContext(agent_id="probe", cell=cell, graph=None, files=FileStateCache(),
                        permissions=PermissionEngine(), network_allowed=False,
                        hooks=[hook]),
        emit=lambda ev: sink(JobEvent(job_id="j", type=ev["type"], data=ev.get("data"))),
    )
    term = asyncio.run(warden.run("what is the tide doing"))
    asyncio.run(close_providers([provider]))

    assert term.reason is StopReason.COMPLETED
    assert "coming in" in str(term.messages)          # seam 1: the tool ran
    assert hook.pre == ["probe_tide"] == hook.post    # seam 3: both stages fired
    assert "tool_result" in sink.events               # seam 4: the sink saw it
    assert "probe_tide tool rather than guessing" in prompt   # seam 7
    assert provider.closed                            # teardown reached it


def test_the_probe_imports_no_core_internals():
    """The restraint IS the test. A seam nothing has gone through is a guess."""
    source = (Path(__file__).resolve().parent.parent
              / "examples" / "plugin_probe" / "__init__.py").read_text(encoding="utf-8")
    forge_imports = {line.split()[1] for line in source.splitlines()
                     if line.startswith("from forge")}
    assert forge_imports == {"forge.agents.prompt", "forge.warden.hooks", "forge.warden.tool"}
