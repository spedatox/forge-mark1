"""Seam 2 — asking the operator (H7).

The safety gate used to be a wall: a gated-but-intended operation returned
"the operator must allow-list it explicitly" and there was no way to do that
mid-job. The job dead-ended on an action the operator would have approved in two
seconds.

The gate is now a checkpoint. What it never becomes is *silent*: every gate match
still refuses to proceed without an explicit yes, and an ask with no answer
degrades to deny. The failure direction is fixed — an unanswered question is a
no, a lost socket is a no, a timeout is a no. Nothing here can turn absence of
an operator into permission.

Two implementations, chosen at assembly. That is the whole shape of a seam: one
protocol, more than one answer, no core module knowing which.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

logger = logging.getLogger("forge.warden")

DEFAULT_ASK_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class Answer:
    approved: bool
    remember: bool = False
    note: str = ""


DENIED = Answer(approved=False)


@runtime_checkable
class PermissionOracle(Protocol):
    async def ask(self, tool_name: str, action_key: str, reason: str) -> Answer:
        """Put one decision to whoever can make it. Must always return."""
        ...


class AutoDenyOracle:
    """No operator is reachable, so the answer is no.

    Used by `serve` mode without a channel, the offline demo, tests, and as the
    fallback wherever a real oracle could not be built. It is not a degraded
    mode — it is Mark I's exact behaviour, which is why shipping the ask before
    any counterpart exists is safe."""

    async def ask(self, tool_name: str, action_key: str, reason: str) -> Answer:
        return Answer(approved=False,
                      note="no operator channel is attached to this job, so gated "
                           "operations cannot be approved while it runs")


class ChannelOracle:
    """Sends the question somewhere and parks until an answer comes back.

    The park happens *inside one tool dispatch*, which is what keeps this from
    touching anything else: interrupt boundaries, transcript shape and batch
    semantics all hold, because from the loop's perspective one tool simply took
    a while. A parked unsafe tool blocks the rest of its sequential batch by
    design — you do not run the next mutation while the operator is still
    thinking about this one."""

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]],
                 timeout_s: float = DEFAULT_ASK_TIMEOUT_S,
                 job_id: str = "", chat_id: str | None = None) -> None:
        self._send = send
        self._timeout = timeout_s
        self._job_id = job_id
        self._chat_id = chat_id
        self._pending: dict[str, asyncio.Future[Answer]] = {}

    async def ask(self, tool_name: str, action_key: str, reason: str) -> Answer:
        ask_id = uuid.uuid4().hex
        future: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()
        self._pending[ask_id] = future
        frame = {
            "type": "permission_request",
            "ask_id": ask_id,
            "job_id": self._job_id,
            "tool": tool_name,
            "action_key": action_key,
            "reason": reason,
            "timeout_s": self._timeout,
        }
        if self._chat_id:
            frame["chat_id"] = self._chat_id
        try:
            await self._send(frame)
        except Exception as e:  # noqa: BLE001 — an unsendable question is unanswered
            logger.warning("permission_request_send_failed", extra={"error": repr(e)})
            self._pending.pop(ask_id, None)
            return Answer(False, note="the operator channel could not be reached")

        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=self._timeout)
        except (asyncio.TimeoutError, TimeoutError):
            logger.info("permission_request_timed_out", extra={"ask_id": ask_id})
            return Answer(False, note=f"no answer within {self._timeout:.0f}s")
        except asyncio.CancelledError:
            raise
        finally:
            self._pending.pop(ask_id, None)

    def resolve(self, ask_id: str, answer: Answer) -> bool:
        """Deliver an answer. False if nothing was waiting for it — a late reply
        to a question that already timed out is not an error, and treating it as
        one would make a slow operator look like a bug."""
        future = self._pending.pop(ask_id, None)
        if future is None or future.done():
            return False
        future.set_result(answer)
        return True

    def abandon_all(self, note: str = "the operator channel closed") -> None:
        """Resolve every parked question to denied. Called on socket teardown:
        losing the channel is not a reason to hang, and it is certainly not a
        reason to proceed."""
        for ask_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result(Answer(False, note=note))
            self._pending.pop(ask_id, None)
