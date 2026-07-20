"""Error classification and transient retry (H4)."""
import asyncio
from typing import Any, AsyncIterator

import pytest
from pydantic import BaseModel

from forge.model.base import TextDelta
from forge.model.errors import ErrorClass, classify, retry_after
from forge.warden.engine import Warden
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import PermissionEngine
from forge.warden.state import ContinueReason, StopReason
from forge.warden.tool import Tool, ToolContext, ToolResult


# ── Doubles for the SDK exception shapes ─────────────────────────────────────
class _Headers(dict):
    pass


class _Response:
    def __init__(self, status: int, headers: dict | None = None) -> None:
        self.status_code = status
        self.headers = _Headers(headers or {})


class ApiError(Exception):
    """Shaped like anthropic.APIStatusError / openai.APIStatusError."""

    def __init__(self, message: str, status: int, headers: dict | None = None) -> None:
        super().__init__(message)
        self.status_code = status
        self.response = _Response(status, headers)


class NudgeArgs(BaseModel):
    pass


class Nudge(Tool):
    name = "nudge"
    description = "do nothing"
    Args = NudgeArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True

    async def call(self, args: NudgeArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult("ok")


class Flaky:
    """Fails `failures` times, then streams normally."""
    model_id = "flaky"

    def __init__(self, error: Exception, failures: int) -> None:
        self.error = error
        self.failures = failures
        self.calls = 0

    async def stream(self, *, system, messages, tools, signal) -> AsyncIterator[Any]:
        self.calls += 1
        if self.calls <= self.failures:
            yield TextDelta("partial…")
            raise self.error
        yield TextDelta("recovered and finished")


def _warden(model, **kwargs) -> Warden:
    kwargs.setdefault("retry_base_delay", 0.001)   # keep the suite fast
    return Warden(system_prompt="", tools={"nudge": Nudge()}, model=model,
                  ctx=ToolContext(agent_id="t", cell=None, graph=None,
                                  files=FileStateCache(), permissions=PermissionEngine(),
                                  network_allowed=False),
                  **kwargs)


# ── Classification ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 529])
def test_transient_statuses(status):
    assert classify(ApiError("upstream said no", status)) is ErrorClass.TRANSIENT


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_statuses(status):
    assert classify(ApiError("nope", status)) is ErrorClass.PERMANENT


@pytest.mark.parametrize("exc", [
    asyncio.TimeoutError(),
    ConnectionResetError("connection reset by peer"),
    Exception("Error: overloaded_error"),
    Exception("upstream connect error: service unavailable"),
])
def test_transient_without_a_status(exc):
    assert classify(exc) is ErrorClass.TRANSIENT


def test_context_length_is_recoverable_even_though_it_arrives_as_a_400():
    """The classification that matters most: as a 400 this reads permanent, and
    calling it permanent turns a recoverable job into a dead one."""
    exc = ApiError("prompt is too long: 210000 tokens > 200000 maximum", 400)
    assert classify(exc) is ErrorClass.RECOVERABLE


def test_an_unknown_failure_gets_the_benefit_of_the_doubt():
    """Unrecognized is treated as transient — one cheap retry settles whether it
    was a blip, and the attempt budget stops it becoming a loop."""
    assert classify(Exception("something nobody has seen before")) is ErrorClass.TRANSIENT


def test_retry_after_is_read_when_the_server_sends_one():
    assert retry_after(ApiError("slow down", 429, {"retry-after": "30"})) == 30.0
    assert retry_after(ApiError("slow down", 429)) is None
    assert retry_after(ApiError("slow down", 429, {"retry-after": "Tue, 1 Jan"})) is None
    assert retry_after(ApiError("slow down", 429, {"retry-after": "99999"})) is None


# ── The loop's behaviour ─────────────────────────────────────────────────────
def test_a_transient_failure_is_retried_and_the_job_completes():
    model = Flaky(ApiError("overloaded", 529), failures=2)
    term = asyncio.run(_warden(model).run("go"))

    assert term.reason is StopReason.COMPLETED
    assert term.final_text == "recovered and finished"
    assert model.calls == 3
    assert [t.reason for t in term.transitions] == [ContinueReason.RETRY_TRANSIENT] * 2


def test_a_permanent_failure_is_not_retried():
    model = Flaky(ApiError("invalid api key", 401), failures=99)
    term = asyncio.run(_warden(model).run("go"))

    assert term.reason is StopReason.ERROR
    assert model.calls == 1
    assert term.transitions == ()


def test_retries_are_bounded():
    model = Flaky(ApiError("overloaded", 529), failures=99)
    term = asyncio.run(_warden(model, retry_attempts=3).run("go"))

    assert term.reason is StopReason.ERROR
    assert model.calls == 4          # the original plus three re-attempts


def test_a_retried_turn_does_not_duplicate_its_partial_text():
    """The failed attempt streamed 'partial…' before dying. It was never
    committed, so the transcript holds only the turn that succeeded."""
    model = Flaky(ApiError("overloaded", 529), failures=1)
    term = asyncio.run(_warden(model).run("go"))

    transcript = str(term.messages)
    assert transcript.count("partial") == 0
    assert transcript.count("recovered and finished") == 1


def test_retries_do_not_consume_the_iteration_budget():
    """A provider having a bad afternoon must not silently shorten the job."""
    model = Flaky(ApiError("overloaded", 529), failures=3)
    term = asyncio.run(_warden(model, max_iterations=2).run("go"))

    assert term.reason is StopReason.COMPLETED
    assert term.iterations == 4       # 3 retry laps plus the one that worked


def test_the_attempt_budget_resets_after_a_good_turn():
    """Six separate blips over an hour are six recoveries, not an exhausted
    budget — the limit bounds one bad patch, not the job's lifetime."""
    class Intermittent:
        model_id = "intermittent"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, *, system, messages, tools, signal):
            self.calls += 1
            # Fail every other call, six times over.
            if self.calls % 2 == 1 and self.calls < 12:
                yield TextDelta("x")
                raise ApiError("overloaded", 529)
            from forge.model.scripted import tool_call
            if self.calls < 12:
                yield TextDelta("working")
                yield tool_call("nudge")
            else:
                yield TextDelta("done")

    model = Intermittent()
    term = asyncio.run(_warden(model, retry_attempts=2, max_iterations=30).run("go"))
    assert term.reason is StopReason.COMPLETED
    assert len([t for t in term.transitions
                if t.reason is ContinueReason.RETRY_TRANSIENT]) == 6


def test_an_abort_during_backoff_stops_promptly():
    """The one moment the loop is doing nothing is the one moment it must still
    be listening — a plain sleep here makes Ctrl-C feel like a hang."""
    signal = asyncio.Event()
    model = Flaky(ApiError("overloaded", 529), failures=99)

    async def scenario():
        warden = _warden(model, signal=signal, retry_base_delay=30.0)
        task = asyncio.create_task(warden.run("go"))
        await asyncio.sleep(0.05)
        signal.set()
        return await asyncio.wait_for(task, timeout=2.0)

    term = asyncio.run(scenario())
    assert term.reason is StopReason.ERROR      # surfaced, not hung
    assert model.calls == 1


def test_the_operator_is_told_a_retry_is_happening():
    events: list[dict] = []

    async def sink(ev):
        events.append(ev)

    model = Flaky(ApiError("overloaded", 529), failures=1)
    asyncio.run(_warden(model, emit=sink).run("go"))

    chunks = "".join(e["data"] for e in events if e["type"] == "chunk")
    assert "connection lost" in chunks and "retrying" in chunks
