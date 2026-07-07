"""Wire contract (§7): the native Forge job envelope and streamed event, plus the
mapping to/from Mark VI's existing agents-WebSocket frames.

Native contract (designed here, documented in the README):

    JobRequest  {agent, task, constraints:{timeout_s, max_iterations, network},
                 repo_path?, job_id}
    JobEvent    {job_id, type, data}   type ∈ started|chunk|tool|tool_result|done|error

`type` deliberately reuses Mark VI's SSE/chat_event vocabulary (chunk / tool /
tool_result / done / error) so a JobEvent re-wraps 1:1 as a Mark VI `chat_event`
and the backend's ExternalAgentProxy stays a dumb pass-through.
"""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class JobConstraints(BaseModel):
    # Execution constraints a job must be able to specify (§7). None → the agent
    # profile's default applies.
    timeout_s: int | None = None        # per-command wall-clock ceiling in the Cell
    max_iterations: int | None = None   # overrides the profile's iteration ceiling
    network: bool = False               # outbound network in the Cell (default: denied, §8)


class JobRequest(BaseModel):
    agent: str                          # which agent to run (must exist in the registry)
    task: str                           # the task text
    constraints: JobConstraints = Field(default_factory=JobConstraints)
    repo_path: str | None = None        # project under work; Cell workspace + graph root
    job_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    # Per-job model override — when set (e.g. by Heartbreaker's model picker),
    # this ref replaces the agent profile's default model for this job only.
    model_override: str | None = None


class JobEvent(BaseModel):
    job_id: str
    type: str                           # started|chunk|tool|tool_result|done|error
    data: Any = None


# ── Mapping to/from Mark VI frames ───────────────────────────────────────────

def job_from_task_dispatch(frame: dict[str, Any], agent_id: str) -> JobRequest:
    """Mark VI `task_dispatch` → JobRequest. Fire-and-await: the stream is later
    collapsed to a single `task_result`."""
    return JobRequest(
        agent=agent_id,
        task=str(frame.get("task", "")),
        repo_path=frame.get("cwd"),
        constraints=JobConstraints(
            network=False,
            max_iterations=None,
        ),
        job_id=str(frame.get("task_id", uuid.uuid4().hex)),
    )


def job_from_chat_request(frame: dict[str, Any], agent_id: str) -> JobRequest:
    """Mark VI `chat_request` → JobRequest. The peer is stateless per turn; the
    task is the last user message in the supplied Anthropic-format history."""
    history = frame.get("history", []) or []
    task = _last_user_text(history)
    return JobRequest(
        agent=agent_id,
        task=task,
        repo_path=frame.get("cwd"),
        job_id=str(frame.get("chat_id", uuid.uuid4().hex)),
        # Heartbreaker's model picker / per-agent pin — None lets the profile
        # default apply; a value overrides it for this turn only.
        model_override=frame.get("model"),
    )


def job_event_to_chat_event(ev: JobEvent) -> dict[str, Any]:
    """JobEvent → Mark VI `chat_event` inner event (1:1 vocabulary)."""
    return {"type": ev.type, "data": ev.data}


def _last_user_text(history: list[dict[str, Any]]) -> str:
    for msg in reversed(history):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            if texts:
                return "\n".join(texts)
    return ""
