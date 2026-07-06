"""The Forge — a standalone privileged execution peer for S.P.E.D.A. Mark VI.

Three layers, never collapsed (§9.3 — Warden reasons, Cell executes):

    Gate     the network boundary. Accepts jobs (native contract or Mark VI's
             agents-WebSocket frames), validates them, streams results back.
    Warden   the parameterized loop engine. One while-true loop, one state
             object, one typed exit. Contains ZERO identity strings (§2) — an
             agent is injected as an AgentConfig + a system prompt.
    Cell     one isolated sandbox per agent, never shared (§9.1). Executes
             shell commands and file ops; the Warden never touches the host.

Optimus and Centurion are not two agents — they are one engine configured with
a different identity, toolset, and Cell policy (§2, §9.4).
"""

__version__ = "0.2.0"  # Mark II
