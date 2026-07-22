"""The Warden loop engine (§3).

One `while True`. One mutable `LoopState`. One typed `Terminal`. The sole stop
condition is 'the model stopped requesting tools'. The interrupt signal is checked
at both boundaries — after the model responds and after tools execute — and yields
a clean, well-formed stop. Tools run concurrently only when every tool in the batch
declares itself concurrency-safe (§4 fail-closed).

The engine contains no identity strings (§2): system prompt, tool set, model, and
Cell all arrive as parameters. It is the same engine for every configured agent;
a new agent is added purely as configuration, never by editing this file.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from forge.model.base import Model, TextDelta, ToolUseRequest, UsageReport
from forge.model.errors import ErrorClass, classify, retry_after
from forge.warden.compaction import (
    ELIDE_AFTER_CYCLES,
    FORCED_CUT_KEEPS,
    FORCED_ELIDE_KEEP,
    KEEP_CYCLES,
    MAX_COMPACT_FAILURES,
    elide_old_tool_results,
    find_cut,
    rebuild,
    render_for_summary,
    summarize,
)
from forge.warden.dispatch import dispatch_tool, to_anthropic_tool_result
from forge.warden.ledger import TokenLedger
from forge.warden.results import enforce_batch_budget
from forge.warden.state import ContinueReason, LoopState, StopReason, Terminal
from forge.warden.tool import Tool, ToolContext, ToolResult

logger = logging.getLogger("forge.warden")

Emit = Callable[[dict[str, Any]], Awaitable[None]]

# Ceiling on tools in flight at once. Bounds the Cell, not the model: the model may
# ask for any number of parallel reads: this decides how many actually run together.
MAX_TOOL_CONCURRENCY = 10

# Consecutive re-attempts at one failed turn, and the first backoff. Four
# attempts at 2s doubling covers ~30s of provider trouble, which is the shape of
# a 529 spike; past that it is an outage and failing loudly beats hanging on.
RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY_S = 2.0
_MAX_BACKOFF_S = 30.0


@dataclass
class _Turn:
    """One model turn, collected in full before any of it reaches the transcript.

    A turn that fails carries its exception here instead of raising, so the loop
    body decides what to do about it at a single, named boundary."""
    text: str = ""
    tool_uses: list[ToolUseRequest] = field(default_factory=list)
    usage: UsageReport | None = None
    error: Exception | None = None

    def assistant_message(self) -> dict[str, Any]:
        """Render the turn as one Anthropic assistant message. Empty turns still
        get a text block — the API rejects empty content."""
        content: list[dict[str, Any]] = []
        if self.text:
            content.append({"type": "text", "text": self.text})
        content.extend({"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input}
                       for tu in self.tool_uses)
        return {"role": "assistant", "content": content or [{"type": "text", "text": ""}]}


async def _noop_emit(_event: dict[str, Any]) -> None:
    return None


class Warden:
    """The parameterized execution loop. Instantiated per job with everything it
    needs injected; holds no agent identity of its own."""

    def __init__(
        self,
        *,
        system_prompt: str,
        tools: dict[str, Tool],
        model: Model,
        ctx: ToolContext,
        max_iterations: int = 30,
        signal: asyncio.Event | None = None,
        emit: Emit | None = None,
        max_tool_concurrency: int = MAX_TOOL_CONCURRENCY,
        ledger: TokenLedger | None = None,
        retry_attempts: int = RETRY_ATTEMPTS,
        retry_base_delay: float = RETRY_BASE_DELAY_S,
        refresh_tools: Callable[[], Awaitable[dict[str, Tool]]] | None = None,
    ) -> None:
        self.system_prompt = system_prompt
        self.tools = tools
        self.model = model
        self.ctx = ctx
        self.max_iterations = max_iterations
        self.signal = signal or asyncio.Event()
        self.emit = emit or _noop_emit
        self._tool_slots = asyncio.Semaphore(max_tool_concurrency)
        # Sized per job from the model's window; the default suits Anthropic.
        self.ledger = ledger or TokenLedger()
        self.retry_attempts = retry_attempts
        self.retry_base_delay = retry_base_delay
        self.refresh_tools = refresh_tools

    async def run(self, task: str) -> Terminal:
        """Drive the loop for one job and return its single typed Terminal."""
        state = LoopState(messages=[{"role": "user", "content": task}], ledger=self.ledger)
        tool_schemas = [t.schema() for t in self.tools.values()]

        while True:
            # ── Budget boundary: the single iteration ceiling (§3). ──────────
            # Retry laps are excluded: the ceiling bounds work attempted, and a
            # provider having a bad afternoon must not silently shorten the job.
            if state.iteration - state.retries >= self.max_iterations:
                await self.emit({"type": "error",
                                 "data": f"reached max_iterations ({self.max_iterations})"})
                return self._terminal(StopReason.MAX_ITERATIONS, state,
                                      error=f"max_iterations ({self.max_iterations}) reached")
            state.iteration += 1

            # ── Make room, before spending a turn discovering there is none. ──
            if state.ledger.should_compact():
                await self._compact(state)

            # ── Seam 1: a source that came up mid-job joins here. ────────────
            if await self._refresh_tools():
                tool_schemas = [t.schema() for t in self.tools.values()]

            # ── Act: stream one model turn, collecting text + tool-use blocks. ─
            turn = await self._stream_turn(state, tool_schemas)

            # ── Failure boundary ─────────────────────────────────────────────
            # Every way a turn can fail arrives here, and nowhere else. That is
            # the point of collecting the turn instead of inlining the stream:
            # recovery (retry a transient, compact and re-attempt) is a decision
            # made at one site over a discarded turn, not an except-block wrapped
            # around half the loop body.
            if turn.error is not None:
                if await self._recover(turn.error, state):
                    continue
                logger.error("model_stream_failed", exc_info=turn.error)
                await self.emit({"type": "error", "data": f"model error: {turn.error}"})
                return self._terminal(
                    StopReason.ERROR, state,
                    error=f"{type(turn.error).__name__}: {turn.error}")

            # The turn streamed cleanly; the bad patch, if there was one, is over.
            state.retry_attempt = 0

            # Account for the turn before the transcript grows. `state.messages`
            # is still exactly what was sent, which is what the estimate has to
            # measure when a provider reports nothing.
            if turn.usage is not None:
                state.ledger.record(turn.usage)
            else:
                state.ledger.estimate(self.system_prompt, state.messages)
            await self.emit({"type": "usage",
                             "data": {**state.ledger.snapshot(),
                                      "iteration": state.iteration}})

            # The turn is committed only now, once the stream has ended cleanly.
            # A turn that failed was never appended, so discarding it needs no
            # transcript surgery — and a retry cannot duplicate its text.
            if turn.text:
                state.last_text = turn.text
            state.messages.append(turn.assistant_message())

            # ── Boundary 1: interrupt checked after the model responds (§3). ──
            if self.signal.is_set():
                # Back-fill tool_results so the transcript stays well-formed even
                # though we did not run the tools (study §4).
                if turn.tool_uses:
                    state.messages.append(self._interrupted_results(turn.tool_uses))
                return self._aborted(state)

            # ── Stop condition: no tool-use blocks → the model is done (§3). ──
            if not turn.tool_uses:
                await self.emit({"type": "done", "data": turn.text})
                return self._terminal(StopReason.COMPLETED, state)

            # ── Observe: run the requested tools (parallel only where safe). ──
            results = await self._run_tools(turn.tool_uses)
            # Each result is already within its own cap; this is the batch as a
            # whole, which no single-result cap can see.
            results = await enforce_batch_budget(turn.tool_uses, results, self.tools, self.ctx)
            result_blocks = [to_anthropic_tool_result(tu.id, res)
                             for tu, res in zip(turn.tool_uses, results)]
            for tu, res in zip(turn.tool_uses, results):
                await self.emit({"type": "tool_result",
                                 "data": {"tool_use_id": tu.id, "is_error": res.is_error,
                                          "content": res.content}})
            state.messages.append({"role": "user", "content": result_blocks})

            # ── Boundary 2: interrupt checked after tools execute (§3). ───────
            if self.signal.is_set():
                return self._aborted(state)

            state.advance(ContinueReason.NEXT_TURN)

    # ── Seam 1: the toolset can change between turns ─────────────────────────
    async def _refresh_tools(self) -> bool:
        """Re-ask the providers. True if the set of names changed.

        Only the *names* are compared, and the toolset is left alone when they
        match. Providers hand back fresh instances every call, so swapping on
        every turn would rebuild the schema array and change the bytes sent to
        the provider — busting the prompt cache on every iteration to achieve
        nothing. The names are what the model can see and what a new source
        actually adds."""
        if self.refresh_tools is None:
            return False
        try:
            latest = await self.refresh_tools()
        except Exception as e:  # noqa: BLE001 — a source that failed to reload
            logger.warning("tool_refresh_failed", extra={"error": repr(e)})
            return False        # keep running with the toolset we have
        if set(latest) == set(self.tools):
            return False
        added = sorted(set(latest) - set(self.tools))
        removed = sorted(set(self.tools) - set(latest))
        logger.info("toolset_changed", extra={"added": added, "removed": removed})
        self.tools = latest
        return True

    # ── Making room ──────────────────────────────────────────────────────────
    async def _compact(self, state: LoopState, forced: bool = False) -> bool:
        """Reclaim context. True if anything was freed.

        Cheap layer first: eliding old tool results costs no model call and
        keeps the reasoning verbatim. Only if that leaves the window still over
        the line does the transcript get summarized, because a summary is lossy
        and granular history is worth keeping when it fits.

        `forced` skips the after-elision threshold check: the caller is
        recovering from a provider that has already refused the request, so
        "probably enough" is not good enough."""
        if state.compact_failures >= MAX_COMPACT_FAILURES:
            logger.warning("compaction_disabled", extra={"failures": state.compact_failures})
            return False

        messages, freed = elide_old_tool_results(
            state.messages, FORCED_ELIDE_KEEP if forced else ELIDE_AFTER_CYCLES)
        if freed:
            state.messages = messages
            # The ledger's figure is from the last API call and cannot see this
            # yet. Adjust provisionally so the next turn's threshold check
            # reflects the reclaim; the next real UsageReport corrects it.
            state.ledger.prompt_tokens = max(0, state.ledger.prompt_tokens - freed // 4)
            await self.emit({"type": "compact",
                             "data": {"stage": "elide", "freed_chars": freed}})
            if not forced and not state.ledger.should_compact():
                self._forget_files()             # the cheap layer was enough
                return True

        cut = None
        for keep in (FORCED_CUT_KEEPS if forced else (KEEP_CYCLES,)):
            cut = find_cut(state.messages, keep)
            if cut is not None:
                break
        if cut is None:
            # Nothing summarizable. Not a failure — a short transcript that is
            # somehow too large has no structure to exploit, and saying so
            # beats burning a model call to discover it.
            logger.info("compaction_found_nothing_to_cut")
            if freed:
                self._forget_files()
            return bool(freed)

        await self.emit({"type": "compact", "data": {"stage": "summarize", "cut": cut}})
        try:
            summary = await summarize(
                self.model, render_for_summary(state.messages[1:cut]), self.signal)
        except Exception as e:  # noqa: BLE001 — a failed rescue must not be fatal
            logger.warning("compaction_call_failed", extra={"error": repr(e)})
            summary = None

        if summary is None:
            state.compact_failures += 1
            if freed:
                self._forget_files()
            return bool(freed)

        state.messages = rebuild(state.messages, cut, summary)
        state.compact_failures = 0
        self._forget_files()
        await self.emit({"type": "compact",
                         "data": {"stage": "done", "messages": len(state.messages)}})
        return True

    def _forget_files(self) -> None:
        """Drop read-before-write grounding after any reclamation.

        Not only after summarizing. Elision removes tool *results*, and a
        `read_file` result is one — so the model can lose a file's contents
        while the cache still reports "you have read this, you may edit it".
        That combination permits a blind edit against remembered text the model
        can no longer see. Any reclamation invalidates the grounding, so any
        reclamation clears it and the next edit has to look again."""
        self.ctx.files.clear()

    # ── Recovery: what to do about a turn that failed ────────────────────────
    async def _recover(self, error: Exception, state: LoopState) -> bool:
        """Decide whether the loop should try again. True means continue.

        The turn that failed was never committed to the transcript, so there is
        nothing to undo: a retry re-sends exactly the prompt the failed attempt
        was given, and cannot duplicate text the operator already saw streamed."""
        kind = classify(error)

        if kind is ErrorClass.RECOVERABLE:
            # The window is full. The same request will fail identically forever,
            # but a *smaller* request may not — so make one smaller and try
            # again. This is the backstop for the proactive threshold above,
            # which works off an estimate and can undershoot.
            if not await self._compact(state, forced=True):
                return False
            state.advance(ContinueReason.RECOVERED_CONTEXT, type(error).__name__)
            return True

        if kind is not ErrorClass.TRANSIENT:
            return False
        if state.retry_attempt >= self.retry_attempts:
            logger.warning("retries_exhausted",
                           extra={"attempts": state.retry_attempt, "error": repr(error)})
            return False

        state.retry_attempt += 1
        state.retries += 1
        delay = self._backoff_delay(state.retry_attempt, retry_after(error))
        logger.info("model_stream_retry",
                    extra={"attempt": state.retry_attempt, "delay_s": round(delay, 1),
                           "error": repr(error)})
        # The operator is watching a stream that just stopped mid-sentence. Say
        # why, or the restart of the visible text looks like the model glitching.
        await self.emit({"type": "chunk",
                         "data": f"\n[connection lost — retrying in {delay:.0f}s "
                                 f"(attempt {state.retry_attempt} of {self.retry_attempts})]\n"})

        if not await self._sleep_unless_interrupted(delay):
            return False        # aborted mid-backoff; fall through to terminate
        state.advance(ContinueReason.RETRY_TRANSIENT,
                      f"{type(error).__name__} (attempt {state.retry_attempt})")
        return True

    def _backoff_delay(self, attempt: int, hint: float | None) -> float:
        """Exponential with jitter, capped. A server's own `retry-after` wins —
        guessing 2 s against a 429 that asked for 30 just earns another 429.

        Jitter matters more than it looks: without it, several agents that hit
        the same rate limit retry in lockstep and re-collide indefinitely."""
        if hint is not None:
            return hint
        delay = min(self.retry_base_delay * (2 ** (attempt - 1)), _MAX_BACKOFF_S)
        return delay * random.uniform(0.75, 1.25)

    async def _sleep_unless_interrupted(self, delay: float) -> bool:
        """Sleep, but stay interruptible. Returns False if the operator aborted.

        A plain sleep here would make an abort during a 30 s backoff feel like a
        hang — the one moment the loop is doing nothing is the one moment it must
        still be listening."""
        try:
            await asyncio.wait_for(self.signal.wait(), timeout=delay)
        except (asyncio.TimeoutError, TimeoutError):
            return True         # slept the full delay undisturbed
        return False            # the signal fired

    # ── One model turn, collected but not yet committed ──────────────────────
    async def _stream_turn(self, state: LoopState, tool_schemas: list[dict[str, Any]]) -> _Turn:
        """Stream one turn into a `_Turn`, converting a stream failure into a
        value rather than letting it unwind the loop. Deltas are emitted as they
        arrive — the operator sees the partial text either way; what a failed turn
        does not get is a place in the transcript."""
        turn = _Turn()
        text_buf: list[str] = []
        try:
            async for ev in self.model.stream(
                system=self.system_prompt,
                messages=state.messages,
                tools=tool_schemas,
                signal=self.signal,
            ):
                if isinstance(ev, TextDelta):
                    text_buf.append(ev.text)
                    await self.emit({"type": "chunk", "data": ev.text})
                elif isinstance(ev, ToolUseRequest):
                    turn.tool_uses.append(ev)
                    await self.emit({"type": "tool",
                                     "data": {"id": ev.id, "name": ev.name, "input": ev.input}})
                elif isinstance(ev, UsageReport):
                    turn.usage = ev
        except Exception as e:  # noqa: BLE001 — classified at the failure boundary
            turn.error = e
        turn.text = "".join(text_buf)
        return turn

    # ── Tool execution: concurrency gated by declared safety (§4). ───────────
    async def _run_tools(self, tool_uses: list[ToolUseRequest]) -> list[ToolResult]:
        """Run one turn's tool batch, preserving the order the model asked for.

        Consecutive concurrency-safe calls form one group that runs together; every
        other call runs alone. Groups execute in emission order, so a read that
        follows a write in the same turn observes that write.

        Partitioning by *runs* rather than by class is the whole point: hoisting
        every safe call ahead of every unsafe one reorders across the batch, and
        because results are re-sorted into request order before they reach the
        transcript, the model has no way to detect that it read a stale file.
        """
        results: dict[str, ToolResult] = {}
        for safe, group in self._partition(tool_uses):
            if safe and len(group) > 1:
                done = await asyncio.gather(*(self._dispatch(tu) for tu in group))
                results.update({tu.id: res for tu, res in zip(group, done)})
            else:
                # Sequential — a lone call, or a mutation that may clobber Cell state.
                for tu in group:
                    results[tu.id] = await self._dispatch(tu)
        return [results[tu.id] for tu in tool_uses]

    def _partition(
        self, tool_uses: list[ToolUseRequest]
    ) -> list[tuple[bool, list[ToolUseRequest]]]:
        """Group the batch into maximal runs of consecutive concurrency-safe calls."""
        groups: list[tuple[bool, list[ToolUseRequest]]] = []
        for tu in tool_uses:
            safe = self._parallel_safe(tu)
            if safe and groups and groups[-1][0]:
                groups[-1][1].append(tu)
            else:
                groups.append((safe, [tu]))
        return groups

    def _parallel_safe(self, tu: ToolUseRequest) -> bool:
        """Whether this specific call may share a group.

        Answering needs the validated arguments, so the schema is parsed here as
        well as in dispatch. Fail closed at every step: an unknown tool, input
        the schema rejects, or a flag check that raises all mean "run it alone".
        Dispatch will produce the proper is_error a moment from now — this only
        decides company, and it is never wrong to keep bad company out."""
        tool = self.tools.get(tu.name)
        if tool is None:
            return False
        try:
            return bool(tool.is_concurrency_safe(tool.Args.model_validate(tu.input)))
        except Exception:  # noqa: BLE001 — see docstring: unsafe is the safe answer
            return False

    async def _dispatch(self, tu: ToolUseRequest) -> ToolResult:
        """One gauntlet run, bounded by the in-flight ceiling. The cap matters at
        the top of a large repo sweep — a 40-call grep batch would otherwise open
        40 simultaneous subprocesses in the Cell."""
        async with self._tool_slots:
            return await dispatch_tool(self.tools, tu.name, tu.input, self.ctx)

    # ── Terminal helpers ─────────────────────────────────────────────────────
    def _interrupted_results(self, tool_uses: list[ToolUseRequest]) -> dict[str, Any]:
        blocks = [to_anthropic_tool_result(
            tu.id, ToolResult("[interrupted before execution]", is_error=True))
            for tu in tool_uses]
        return {"role": "user", "content": blocks}

    def _aborted(self, state: LoopState) -> Terminal:
        state.messages.append(
            {"role": "user", "content": "[the operator interrupted this run]"})
        return self._terminal(StopReason.ABORTED, state)

    def _terminal(self, reason: StopReason, state: LoopState, error: str | None = None) -> Terminal:
        return Terminal(reason=reason, final_text=state.last_text,
                        iterations=state.iteration, error=error, messages=state.messages,
                        transitions=tuple(state.transitions),
                        usage=state.ledger.snapshot())
