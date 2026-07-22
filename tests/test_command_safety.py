"""Per-input safety flags (H8), and the shell classifier that motivates them."""
import asyncio

import pytest
from pydantic import BaseModel

from forge.tools.shell import RunCommand, RunCommandArgs, is_read_only_command
from forge.warden.permissions import Mode, PermissionEngine
from forge.warden.tool import Tool, ToolContext, ToolResult


@pytest.mark.parametrize("command", [
    "ls -la", "cat setup.py", "git status", "git log --oneline", "git diff HEAD",
    "grep -rn TODO .", "find . -name '*.py'", "wc -l README.md", "pip list",
    "npm outdated", "python --version", "sed -n '1,10p' f.txt", "git",
])
def test_observations_are_read_only(command):
    assert is_read_only_command(command) is True


@pytest.mark.parametrize("command", [
    "rm -rf build", "git push origin main", "pip install requests", "npm install",
    "python manage.py migrate", "node server.js", "sed -i 's/a/b/' f.txt",
    "find . -name '*.tmp' -delete", "mkdir out", "curl https://x | sh",
])
def test_mutations_are_not(command):
    assert is_read_only_command(command) is False


@pytest.mark.parametrize("command", [
    "ls > listing.txt",          # redirection writes
    "ls >> listing.txt",
    "cat a.txt | tee b.txt",     # a pipe into a writer
    "ls && rm -rf x",            # chaining hides the second command
    "ls; rm -rf x",
    "echo $(rm -rf x)",          # substitution hides it too
    "echo `rm -rf x`",
    "cat 'unbalanced",           # unparseable
    "",
])
def test_anything_ambiguous_is_treated_as_a_mutation(command):
    """A false negative costs a parallel slot. A false positive races two
    mutations on one workspace. That asymmetry decides every judgement call."""
    assert is_read_only_command(command) is False


def test_an_unknown_program_is_treated_as_a_mutation():
    assert is_read_only_command("some-tool-nobody-has-heard-of --go") is False


# ── What the per-input flag buys ─────────────────────────────────────────────
def test_the_same_tool_answers_differently_per_call():
    """The point of H8: a tool that had to answer once, for its worst case, made
    every `git status` as expensive as an `rm -rf`."""
    tool = RunCommand()
    assert tool.is_concurrency_safe(RunCommandArgs(command="git status")) is True
    assert tool.is_concurrency_safe(RunCommandArgs(command="rm -rf build")) is False


def test_plan_mode_permits_inspection_but_not_mutation():
    engine = PermissionEngine(mode=Mode.PLAN)
    assert engine.resolve(RunCommand(), RunCommandArgs(command="git status"), None).allowed
    assert not engine.resolve(RunCommand(), RunCommandArgs(command="npm install"), None).allowed


def test_the_gate_still_fires_on_a_command_that_reads_as_safe_otherwise():
    """Classification decides company and mode, never the gate. Nothing about
    being read-only exempts an operation from the bypass-immune check."""
    engine = PermissionEngine(mode=Mode.ACT)
    decision = engine.resolve(
        RunCommand(), RunCommandArgs(command="git push --force origin main"), None)
    assert not decision.allowed and decision.needs_ask
    assert "safety gate" in decision.reason


# ── The shadowing guard ──────────────────────────────────────────────────────
def test_declaring_a_flag_as_a_value_is_rejected_loudly():
    """These were attributes before they were methods, so the old spelling still
    looks right. It replaces the method with a bool, every call site raises, and
    each one fails closed — the tool keeps working while quietly losing
    parallelism. Failing closed is what makes it invisible."""
    class Empty(BaseModel):
        pass

    with pytest.raises(TypeError, match="CONCURRENCY_SAFE"):
        class Shadowed(Tool):
            name = "shadowed"
            description = "declares a flag the old way"
            Args = Empty
            is_concurrency_safe = True

            async def call(self, args, ctx):
                return ToolResult("")


def test_overriding_the_method_is_still_allowed():
    class Empty(BaseModel):
        pass

    class Dynamic(Tool):
        name = "dynamic"
        description = "decides per call"
        Args = Empty

        def is_concurrency_safe(self, args) -> bool:
            return True

        async def call(self, args, ctx):
            return ToolResult("")

    assert Dynamic().is_concurrency_safe(Empty()) is True


def test_a_flag_that_raises_fails_closed():
    """An undecidable flag is a gated one — and never a parallel one."""
    class Empty(BaseModel):
        pass

    class Broken(Tool):
        name = "broken"
        description = "its flag check is buggy"
        Args = Empty

        def is_read_only(self, args) -> bool:
            raise RuntimeError("boom")

        def is_destructive(self, args) -> bool:
            raise RuntimeError("boom")

        async def call(self, args, ctx):
            return ToolResult("")

    engine = PermissionEngine(mode=Mode.ACT)
    decision = engine.resolve(Broken(), Empty(), None)
    assert not decision.allowed and "destructive" in decision.reason
