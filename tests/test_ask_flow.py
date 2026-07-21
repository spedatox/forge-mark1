"""The ask round-trip (H7): the gate as a checkpoint that is never silent."""
import asyncio
import json

import pytest

from forge.model.scripted import ScriptedModel, tool_call
from forge.tools.shell import RunCommand, RunCommandArgs
from forge.warden.dispatch import dispatch_tool
from forge.warden.engine import Warden
from forge.warden.filestate import FileStateCache
from forge.warden.oracle import Answer, AutoDenyOracle, ChannelOracle
from forge.warden.permissions import AllowList, Mode, PermissionEngine
from forge.warden.state import StopReason
from forge.warden.tool import ToolContext

FORCE_PUSH = "git push --force origin main"


class Recording:
    """An oracle that answers however the test says, and remembers being asked."""

    def __init__(self, answer: Answer) -> None:
        self.answer = answer
        self.asked: list[tuple[str, str]] = []

    async def ask(self, tool_name, action_key, reason):
        self.asked.append((tool_name, action_key))
        return self.answer


class FakeCell:
    host_path = None

    async def run(self, command, timeout=None, env=None):
        from forge.cell.base import CommandResult
        return CommandResult(stdout="pushed", stderr="", exit_code=0)


def _ctx(oracle=None, allowlist=None, mode=Mode.ACT) -> ToolContext:
    return ToolContext(agent_id="t", cell=FakeCell(), graph=None, files=FileStateCache(),
                       permissions=PermissionEngine(mode=mode,
                                                    allowlist=allowlist or AllowList()),
                       network_allowed=False, oracle=oracle)


def _dispatch(ctx, command=FORCE_PUSH):
    return asyncio.run(dispatch_tool({"run_command": RunCommand()}, "run_command",
                                     {"command": command}, ctx))


# ── The chain ────────────────────────────────────────────────────────────────
def test_a_gated_action_asks_rather_than_dead_ending():
    engine = PermissionEngine(mode=Mode.ACT)
    decision = engine.resolve(RunCommand(), RunCommandArgs(command=FORCE_PUSH), None)
    assert decision.needs_ask and decision.source == "gate"


def test_an_ungated_action_is_never_asked_about():
    oracle = Recording(Answer(True))
    assert not _dispatch(_ctx(oracle), "pytest -q").is_error
    assert oracle.asked == []


def test_plan_mode_denies_without_asking():
    """Review means review. Asking mid-review to break the review is not a
    question worth putting to an operator who chose it."""
    oracle = Recording(Answer(True))
    result = _dispatch(_ctx(oracle, mode=Mode.PLAN), "npm install")
    assert result.is_error and oracle.asked == []


# ── The answers ──────────────────────────────────────────────────────────────
def test_approval_lets_the_action_run():
    oracle = Recording(Answer(approved=True))
    result = _dispatch(_ctx(oracle))
    assert not result.is_error and "pushed" in result.content
    assert oracle.asked == [("run_command", FORCE_PUSH)]


def test_refusal_becomes_a_correctable_error():
    oracle = Recording(Answer(approved=False, note="not on main"))
    result = _dispatch(_ctx(oracle))
    assert result.is_error and "not on main" in result.content


def test_no_oracle_denies_exactly_as_before():
    """With nothing listening the behaviour is Mark I's, which is what makes the
    ask safe to ship before any counterpart exists."""
    result = _dispatch(_ctx(None))
    assert result.is_error and "No operator channel" in result.content


def test_the_auto_deny_oracle_says_no():
    result = _dispatch(_ctx(AutoDenyOracle()))
    assert result.is_error


def test_an_oracle_that_raises_does_not_approve():
    class Broken:
        async def ask(self, *a):
            raise RuntimeError("boom")

    assert _dispatch(_ctx(Broken())).is_error


# ── Remembering ──────────────────────────────────────────────────────────────
def test_remember_skips_the_second_ask():
    oracle = Recording(Answer(approved=True, remember=True))
    ctx = _ctx(oracle)
    assert not _dispatch(ctx).is_error
    assert not _dispatch(ctx).is_error
    assert len(oracle.asked) == 1, "the standing approval should answer the second"


def test_remember_records_the_exact_action_not_a_pattern():
    oracle = Recording(Answer(approved=True, remember=True))
    ctx = _ctx(oracle)
    _dispatch(ctx)
    assert ctx.permissions.allowlist.entries == {f"run_command:{FORCE_PUSH}"}
    # A different force-push is a different decision.
    _dispatch(ctx, "git push --force origin production")
    assert len(oracle.asked) == 2


def test_approving_without_remember_asks_again():
    oracle = Recording(Answer(approved=True, remember=False))
    ctx = _ctx(oracle)
    _dispatch(ctx)
    _dispatch(ctx)
    assert len(oracle.asked) == 2


# ── The invariant ────────────────────────────────────────────────────────────
def test_a_blanket_grant_cannot_satisfy_the_gate():
    """The bypass-immune property, restated for the ask era: a wildcard is a
    convenience about ordinary operations, not a decision anybody made about an
    irreversible one."""
    for entry in ("run_command", "run_command:git push*", "run_command:*"):
        oracle = Recording(Answer(approved=False))
        ctx = _ctx(oracle, AllowList({entry}))
        assert _dispatch(ctx).is_error, f"{entry!r} must not bypass the gate"
        assert oracle.asked, f"{entry!r} should still have produced an ask"


def test_an_exact_standing_approval_does_satisfy_it():
    oracle = Recording(Answer(approved=False))
    ctx = _ctx(oracle, AllowList({f"run_command:{FORCE_PUSH}"}))
    assert not _dispatch(ctx).is_error
    assert oracle.asked == [], "a decision already on file is not re-asked"


# ── Persistence ──────────────────────────────────────────────────────────────
def test_approvals_survive_the_job_that_granted_them(tmp_path):
    store = tmp_path / "allowlist.json"
    first = AllowList.load(store)
    first.add(f"run_command:{FORCE_PUSH}")

    assert AllowList.load(store).entries == {f"run_command:{FORCE_PUSH}"}


def test_a_corrupt_store_forgets_rather_than_invents(tmp_path):
    """Forgetting costs a few extra prompts. Inventing grants permission nobody
    gave, so a half-parsed file must never become a standing approval."""
    store = tmp_path / "allowlist.json"
    store.write_text('{"entries": ["run_command:rm -rf /"', encoding="utf-8")
    assert AllowList.load(store).entries == set()


def test_a_missing_store_is_not_an_error(tmp_path):
    assert AllowList.load(tmp_path / "nope.json").entries == set()


def test_the_store_is_written_atomically(tmp_path):
    """A crash mid-write must leave the previous file intact rather than a
    truncated one that reads as 'no approvals' on the next start."""
    store = tmp_path / "allowlist.json"
    allow = AllowList.load(store)
    allow.add("run_command:pytest -q")
    assert json.loads(store.read_text(encoding="utf-8"))["entries"] == ["run_command:pytest -q"]
    assert not list(tmp_path.glob("*.tmp")), "the temporary file must not survive"


# ── The channel ──────────────────────────────────────────────────────────────
def test_a_parked_ask_resolves_when_the_answer_arrives():
    sent: list[dict] = []

    async def send(frame):
        sent.append(frame)

    oracle = ChannelOracle(send, timeout_s=5.0, job_id="j1", chat_id="c1")

    async def scenario():
        task = asyncio.create_task(oracle.ask("run_command", FORCE_PUSH, "gated"))
        await asyncio.sleep(0.02)
        oracle.resolve(sent[0]["ask_id"], Answer(approved=True, remember=True))
        return await asyncio.wait_for(task, timeout=2.0)

    answer = asyncio.run(scenario())
    assert answer.approved and answer.remember
    assert sent[0]["type"] == "permission_request"
    assert sent[0]["chat_id"] == "c1" and sent[0]["action_key"] == FORCE_PUSH


def test_an_unanswered_ask_times_out_into_a_denial():
    async def send(frame):
        return None

    oracle = ChannelOracle(send, timeout_s=0.05)
    answer = asyncio.run(oracle.ask("run_command", FORCE_PUSH, "gated"))
    assert not answer.approved and "no answer within" in answer.note


def test_losing_the_channel_denies_everything_parked():
    sent: list[dict] = []

    async def send(frame):
        sent.append(frame)

    oracle = ChannelOracle(send, timeout_s=30.0)

    async def scenario():
        task = asyncio.create_task(oracle.ask("run_command", FORCE_PUSH, "gated"))
        await asyncio.sleep(0.02)
        oracle.abandon_all()
        return await asyncio.wait_for(task, timeout=2.0)

    answer = asyncio.run(scenario())
    assert not answer.approved, "a lost socket is a no, never a yes"


def test_a_late_answer_is_dropped_not_an_error():
    async def send(frame):
        return None

    oracle = ChannelOracle(send, timeout_s=0.01)
    assert oracle.resolve("never-asked", Answer(True)) is False


def test_an_unsendable_question_is_a_denial():
    async def send(frame):
        raise ConnectionError("socket gone")

    oracle = ChannelOracle(send, timeout_s=5.0)
    answer = asyncio.run(oracle.ask("run_command", FORCE_PUSH, "gated"))
    assert not answer.approved


# ── Through the loop ─────────────────────────────────────────────────────────
def test_a_denial_leaves_the_loop_running():
    """The model is told it could not do the thing and carries on — a refusal is
    a tool result, not a terminal."""
    oracle = Recording(Answer(approved=False, note="not today"))
    warden = Warden(
        system_prompt="", tools={"run_command": RunCommand()},
        model=ScriptedModel([
            lambda m: ("pushing", [tool_call("run_command", command=FORCE_PUSH)]),
            lambda m: ("could not push, finishing up", []),
        ]),
        ctx=_ctx(oracle),
    )
    term = asyncio.run(warden.run("push it"))
    assert term.reason is StopReason.COMPLETED
    assert "could not push" in term.final_text
