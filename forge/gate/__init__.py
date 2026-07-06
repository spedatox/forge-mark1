"""The Gate — the Forge's network boundary (§7).

Accepts job requests (a native Forge envelope, or Mark VI's agents-WebSocket
frames mapped onto it), validates them, builds a fresh unshared Cell + Graph +
Warden per job, and streams results back as they happen. Two front doors sit on
one runner:

    peer.py    connects to Mark VI as a WebSocket peer and speaks its protocol
               (agent_register / task_dispatch / chat_request → task_result /
               chat_event). This is the production path (§9.2).
    server.py  a standalone WS server speaking the native JobRequest/JobEvent
               contract — for running and testing the Forge without Mark VI.
"""
from forge.gate.protocol import JobRequest, JobConstraints, JobEvent
from forge.gate.runner import run_job

__all__ = ["JobRequest", "JobConstraints", "JobEvent", "run_job"]
