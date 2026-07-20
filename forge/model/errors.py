"""Classifying a failed model turn (H4).

One `except` that turns every failure into a terminal is the reason a 25-iteration
job dies to a single 529. But "retry everything" is worse: retrying a 401 four
times with backoff wastes half a minute to arrive at the same wrong answer, and
retrying a context-length error can never succeed no matter how many times it is
attempted.

Three classes, because there are three genuinely different responses:

| Class | Response | Examples |
|---|---|---|
| `TRANSIENT` | retry the same model after a backoff | 429, 500, 502, 503, 529, overloaded, timeouts, resets |
| `RECOVERABLE` | change something, then re-attempt | context length exceeded |
| `PERMANENT` | surface immediately | 400 bad request, 401/403 auth, 404 |

`RECOVERABLE` is the class Forge had no vocabulary for. It is not transient — the
same request will fail identically forever — but it is not fatal either, because
the *request* can be made smaller. Compaction is what acts on it (H6).

Detection prefers typed SDK exceptions (`status_code`), falls back to exception
type, and only then sniffs strings — the OpenAI-compatible providers wrap errors
loosely enough that text is sometimes all there is. An unrecognized failure is
treated as transient **once**: one cheap retry distinguishes a blip from a real
fault, and the attempt budget stops it becoming a loop.
"""
from __future__ import annotations

import asyncio
import enum
import re
from typing import Any


class ErrorClass(str, enum.Enum):
    TRANSIENT = "transient"
    RECOVERABLE = "recoverable"
    PERMANENT = "permanent"


# 408 request-timeout and 409 conflict are retryable; 429 is rate limiting, which
# is the single most common one in practice. 529 is Anthropic's overloaded.
_TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
_PERMANENT_STATUS = frozenset({400, 401, 403, 404, 405, 413, 422})

# Checked before status, because a context-length error arrives as a 400 and
# would otherwise be classified permanent — which is exactly the mistake that
# turns a recoverable job into a dead one.
_CONTEXT_PATTERNS = re.compile(
    r"context[ _-]?length|context[ _-]?window|prompt is too long|"
    r"maximum context|too many tokens|reduce the length",
    re.IGNORECASE)

_TRANSIENT_PATTERNS = re.compile(
    r"overloaded|rate[ _-]?limit|too many requests|timed? ?out|timeout|"
    r"temporarily unavailable|service unavailable|bad gateway|"
    r"connection (reset|aborted|closed|error)|remote end closed|"
    r"server disconnected|internal server error|try again",
    re.IGNORECASE)

_PERMANENT_PATTERNS = re.compile(
    r"invalid[ _-]?api[ _-]?key|authentication|unauthorized|forbidden|"
    r"permission denied|not found|invalid request|unsupported",
    re.IGNORECASE)


def _status_of(exc: BaseException) -> int | None:
    """The HTTP status an SDK exception carries, if it carries one. Both the
    Anthropic and OpenAI clients expose `status_code`; some wrappers only keep
    the response object."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def classify(exc: BaseException) -> ErrorClass:
    """Decide what kind of failure this is. Never raises."""
    text = f"{type(exc).__name__}: {exc}"

    # Context length first — it masquerades as a 400.
    if _CONTEXT_PATTERNS.search(text):
        return ErrorClass.RECOVERABLE

    status = _status_of(exc)
    if status is not None:
        if status in _TRANSIENT_STATUS:
            return ErrorClass.TRANSIENT
        if status in _PERMANENT_STATUS:
            return ErrorClass.PERMANENT
        return ErrorClass.TRANSIENT if status >= 500 else ErrorClass.PERMANENT

    # Transport-level failures: the network, not the request.
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return ErrorClass.TRANSIENT

    if _TRANSIENT_PATTERNS.search(text):
        return ErrorClass.TRANSIENT
    if _PERMANENT_PATTERNS.search(text):
        return ErrorClass.PERMANENT

    # Unrecognized. One retry is cheap and settles whether it was a blip; the
    # attempt budget is what keeps "unknown" from becoming an infinite loop.
    return ErrorClass.TRANSIENT


def retry_after(exc: BaseException) -> float | None:
    """The server's own backoff instruction, when it sent one. Honouring it beats
    guessing — a 429 with `retry-after: 30` retried at 2 s just gets another
    429."""
    response = getattr(exc, "response", None)
    headers: Any = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except (AttributeError, TypeError):
        return None
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None          # HTTP-date form; fall back to the computed backoff
    return seconds if 0 <= seconds <= 300 else None
