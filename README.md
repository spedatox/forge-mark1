<div align="center">

# The Forge

**A standalone, single-operator execution peer for S.P.E.D.A. Mark VI.**

Privileged execution — shell, generated code, and security tooling — isolated in
its own process so the main backend never inherits its threat model.

[![python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![tests](https://img.shields.io/badge/tests-13%20passing-brightgreen.svg)](tests/test_forge.py)
[![isolation](https://img.shields.io/badge/cell-docker%20%7C%20subprocess-informational.svg)](#5-the-cell--sandbox-isolation)
[![status](https://img.shields.io/badge/build-Mark%20II-success.svg)](#)

</div>

---

## Table of contents

1. [Overview](#1-overview)
2. [Quickstart](#2-quickstart)
3. [Architecture](#3-architecture)
4. [The execution loop](#4-the-execution-loop)
5. [The Cell — sandbox isolation](#5-the-cell--sandbox-isolation)
6. [Codebase understanding — Graphify](#6-codebase-understanding--graphify)
7. [Security & permission model](#7-security--permission-model)
8. [Wire protocol](#8-wire-protocol)
9. [Agents — the rebrandable core](#9-agents--the-rebrandable-core)
10. [Configuration reference](#10-configuration-reference)
11. [Connecting to S.P.E.D.A. Mark VI](#11-connecting-to-speda-mark-vi)
12. [Development](#12-development)
13. [Design principles](#13-design-principles)
14. [Project layout](#14-project-layout)

---

## 1. Overview

The Forge hosts the two S.P.E.D.A. agents that require privileged execution:

| Agent | Role | Status |
|-------|------|--------|
| **Optimus** | Coding agent — writes and runs code, reads results, iterates | ✅ Built |
| **Centurion** | Security agent — runs scans and tooling, reads results, adapts | ⏳ Addable as a second config (see [§9](#9-agents--the-rebrandable-core)) |

Both run the identical loop — **act → observe → evaluate → adapt** — until the
task is done or a give-up condition fires. They are **not two agents**: they are
**one parameterized engine** configured with a different identity, toolset, and
sandbox. Building a second agent adds a configuration folder, never engine code.

### Design lineage

This is **Mark II**. Mark I was a 1:1 port of a general-purpose coding agent and
inherited an apparatus built for millions of untrusted users — telemetry, feature
flags, five permission modes, model-fallback ladders, layered recovery state
machines. For a single trusted operator that was pure weight.

Mark II studies the same source material *for architectural shape only*, adopts
the patterns that are genuinely load-bearing, and deliberately omits everything
that exists to serve an audience the Forge does not have. The full rationale is in
[§13](#13-design-principles).

---

## 2. Quickstart

```bash
# 1. Install
pip install -e .          # core deps: pydantic, websockets, anthropic
pip install graphifyy     # optional: the codebase-graph capability (§6)
                          # Docker is optional — the Cell falls back to a
                          # subprocess sandbox when the daemon is absent.

# 2. Run the offline end-to-end demo (no API key, no network required)
python -m forge demo
```

The demo provisions a tiny repository containing a deliberate bug, then drives
Optimus's full loop against it: it maps the codebase through the **Graphify
knowledge graph**, runs the failing check, reads the source, applies a fix,
re-runs the check to confirm, and reports — with tool activity **streaming live**
and the fix verified on disk. It uses a deterministic model stand-in whose steps
branch on the running transcript, so the loop, sandbox, graph, and streaming are
all exercised without a key.

Expected tail:

```
=== TERMINAL ===
reason: completed
iterations: 7
final: Done. The bug in `add` was a subtraction instead of an addition; ...

fix present on disk: True   loop completed: True
=== DEMO PASSED ===
```

---

## 3. Architecture

The Forge is three layers with one hard rule between them: **the Warden reasons,
the Cell executes — never collapsed for convenience.**

```
                     ┌──────────────────────────────────────────────┐
   S.P.E.D.A.        │  GATE                                        │
   Mark VI  ──WS──►  │  Network boundary. Validates job requests,    │
   (or a direct      │  streams results. Speaks a native contract    │
    client)          │  and Mark VI's agents-WebSocket protocol.     │
                     └───────────────────────┬──────────────────────┘
                                             │ run_job(request) → stream
                     ┌───────────────────────▼──────────────────────┐
                     │  WARDEN                                       │
                     │  The parameterized loop engine. One while-    │
                     │  true loop, one state object, one typed exit. │
                     │  Holds zero agent identity.                   │
                     │                                               │
                     │   ┌─────────────────┐   ┌──────────────────┐  │
                     │   │ Graphify sidecar│   │ Tool boundary    │  │
                     │   │ (MCP, read-side │   │ validate→permit  │  │
                     │   │  code context)  │   │ →execute         │  │
                     │   └─────────────────┘   └────────┬─────────┘  │
                     └────────────────────────────────── │───────────┘
                                                         │ Cell.run / read / write / reset
                     ┌───────────────────────────────────▼──────────┐
                     │  CELL                                        │
                     │  One isolated sandbox per agent, never        │
                     │  shared. No network by default; CPU / memory  │
                     │  / time capped. Docker or subprocess backend. │
                     └───────────────────────────────────────────────┘
```

| Layer | Package | Responsibility |
|-------|---------|----------------|
| **Gate** | `forge/gate/` | Accepts jobs (native envelope or Mark VI frames), validates, streams `JobEvent`s. Two front doors: `server.py` (standalone) and `peer.py` (Mark VI). |
| **Warden** | `forge/warden/` | The execution loop, the tool boundary, the permission engine. Injected with an `AgentConfig`; contains no agent names. |
| **Cell** | `forge/cell/` | The `run / write / read / reset` sandbox contract and its Docker and subprocess backends. |
| **Graphify** | `forge/graph/` | A Warden-side MCP sidecar exposing a queryable codebase graph. |
| **Agents** | `forge/agents/` | The rebrandable boundary: one folder per agent, two files each. |
| **Model** | `forge/model/` | The reasoning client — Anthropic in production, a deterministic stand-in for the demo and tests. |

### Why Python

The entire S.P.E.D.A. network is Python/FastAPI; the peer must speak Mark VI's
WebSocket protocol; Graphify is a Python package; and the reference architecture
maps cleanly onto Python primitives (Pydantic for schema validation,
`asyncio.Event` for cooperative interruption). One language across the whole
system keeps one mental model.

---

## 4. The execution loop

The Warden is a single `while True` over one mutable state object, returning
exactly one typed result.

```python
Terminal(reason: StopReason, final_text, iterations, error, messages)

class StopReason(Enum):
    COMPLETED       # the model stopped requesting tools — the only success path
    ABORTED         # the interrupt signal fired at a boundary
    MAX_ITERATIONS  # the single iteration ceiling was reached
    ERROR           # an unrecoverable failure, surfaced verbatim
```

Guarantees the loop enforces:

- **One stop condition.** A turn that requests no tools ends the loop. There is no
  separate "are we done?" classifier.
- **One iteration ceiling.** A single `max_iterations` guard (default 30), not a
  set of overlapping limits.
- **Clean interruption.** An `asyncio.Event` is checked at both natural
  boundaries — after the model responds and after tools execute — and produces a
  well-formed stop with a valid transcript, never a dangling half-turn.
- **Safe concurrency.** Tools declared concurrency-safe run in parallel; anything
  else runs sequentially. Safety is opt-in, so an undeclared tool is serialized by
  default.

Every iteration streams `JobEvent`s (`chunk`, `tool`, `tool_result`, `done`,
`error`) as they happen, so a client observes progress throughout a long job
rather than waiting for a final blob.

### The tool boundary

A tool, as the model sees it, is exactly three things: a **name**, a
**description**, and an **input schema** (a Pydantic model rendered to JSON
Schema). Everything else — whether it is read-only, concurrency-safe, or
destructive; its result-size cap; its permission logic — is harness-side and
invisible to the model.

Defaults are **fail-closed**: a new tool is assumed *not* read-only, *not*
concurrency-safe, *not* destructive, and *not* automatically permitted unless it
declares otherwise.

Dispatch is a fixed, ordered pipeline — **validate → permit → execute** — and
**every** failure at every stage becomes an identical `is_error` tool result fed
back to the model. An unknown tool, malformed input, a permission denial, and a
thrown exception are indistinguishable to the loop: all are data the model can
react to on its next turn. No exception escapes the loop. Oversized results are
spilled to a file in the sandbox and replaced with a preview plus a path, so a
single large result cannot blow the context window.

---

## 5. The Cell — sandbox isolation

Every backend implements one contract:

```python
Cell.run(command, timeout, env) -> CommandResult(stdout, stderr, exit_code, timed_out)
Cell.write(path, content)
Cell.read(path)
Cell.reset()
```

**Default posture:** no outbound network unless the job explicitly requests it;
CPU, memory, PID count, and wall-clock all capped; output byte-capped; process
runs non-root. **One Cell per agent, never shared** — the Gate builds a fresh one
per job, and container names / workspace directories are per-agent, so two agents
can never touch the same sandbox.

### `DockerCell` — production default

One throwaway container per agent, started once (`sleep infinity`) with commands
executed via `docker exec` so filesystem and installed-package state persist
across calls like a real machine.

| Control | Flag |
|---------|------|
| No network (default) | `--network none` |
| Memory ceiling | `--memory <mb>` |
| CPU ceiling | `--cpus <n>` |
| Fork-bomb guard | `--pids-limit <n>` |
| Non-root | `--user 1000:1000` |
| Drop capabilities | `--cap-drop ALL` |
| No privilege escalation | `--security-opt no-new-privileges` |
| Workspace | single bind mount of the job's repo to `/workspace` |

`reset()` destroys and recreates the container.

> **Rationale.** Docker is the same isolation technology the S.P.E.D.A. backend
> already sandboxes with, so the two systems share one operational model. It
> provides a real kernel-namespace and cgroup boundary with resource caps as a
> single, well-understood dependency. A microVM (Firecracker / gVisor) would be a
> stronger boundary but adds a KVM/Linux requirement the single-operator Forge
> does not need for its threat model. The trade-off is deliberate and stated here
> rather than hidden.

### `SubprocessCell` — reduced-isolation fallback

The same four-method contract, but the boundary is a per-agent **workspace jail**
with wall-clock and output caps, not a kernel namespace. It strips provider and
API secrets from the command environment and best-effort black-holes outbound
network via proxy variables. It is **honest about its limits**: a workspace jail,
not a defense against hostile code. The factory selects it automatically when
Docker is unavailable — which is why the demo runs on a machine without Docker
installed.

Backend selection is governed by `FORGE_CELL_BACKEND` (`docker` | `subprocess` |
`auto`); `auto` uses Docker when the daemon answers and the subprocess backend
otherwise.

---

## 6. Codebase understanding — Graphify

The Forge does not build a bespoke code indexer. It uses
[**Graphify**](https://github.com/Graphify-Labs/graphify) (MIT; PyPI `graphifyy`)
directly as a dependency, wired in as a **Warden-side capability** — read-side
context the agent queries, never something that runs inside the sandbox.

```
1. Index once per session   python -m graphify <repo> --no-label
                            → graphify-out/graph.json  (tree-sitter AST; no LLM call)
2. Serve                    python -m graphify.serve graph.json --transport stdio
3. Query over MCP           forge/graph/sidecar.py speaks JSON-RPC to the sidecar
```

The Warden exposes three read-only, concurrency-safe tools backed by the sidecar:

| Tool | Purpose |
|------|---------|
| `graph_query` | Natural-language / keyword search across the graph (BFS or DFS) |
| `graph_path` | Shortest relationship path between two named entities |
| `graph_overview` | Graph statistics plus the most-connected "god nodes" |

This lets the agent ask *"where is `add` defined and who calls it?"* instead of
re-reading whole files — which directly reduces reliance on aggressive context
compaction. Context management is therefore intentionally minimal: recent messages
plus per-result size caps, with no speculative compaction cascade. If indexing or
the MCP handshake fails, the graph tools return an `is_error` result instructing
the model to fall back to reading files; a missing graph never fails a job.

---

## 7. Security & permission model

One short precedence chain, ending in a hard, non-bypassable gate:

```
1. Session allow-list          operator's known-safe repeats            → allow
2. Tool's own check            tool-specific opinion                    → allow / deny
3. SAFETY GATE  (bypass-immune)  irreversible / high-blast-radius ops    → deny
                                 even if 1–2 said allow
4. Mode                        plan → deny mutations; act → allow
```

- **One working mode** — `act`, with the safety gate always on — plus an optional
  read-only `plan` mode for reviewing an agent's intended actions before it acts.
  There is no LLM risk classifier and no denial-tracking: the operator is the risk
  assessor.
- **The safety gate is bypass-immune.** It fires for operations that are
  irreversible or high-blast-radius regardless of any allow-list entry or mode. It
  is tool-agnostic — it inspects a tool's `destructive` flag and any `path` /
  `command` argument against protected-path and dangerous-command patterns:

  | Protected paths | Dangerous commands |
  |-----------------|--------------------|
  | `.git/` internals, `.ssh`, `.aws`, `.kube` | `rm -rf`, `git push --force`, `git reset --hard` |
  | `.env`, credentials, `*.pem`, `*.key`, `.netrc` | `curl … \| sh`, `wget … \| sh` |
  | shell rc/profile files (`.bashrc`, PowerShell profile, …) | `mkfs`, `dd`, `sudo`, `shutdown`, fork bombs |

  A gated operation proceeds only if the operator explicitly allow-lists it.
- **No layered rule sources.** A single session allow-list — no
  policy/project/enterprise layering, because there is no second party to govern.

---

## 8. Wire protocol

The job contract was designed here, and designed to map 1:1 onto the protocol
Mark VI already speaks so the Forge is a drop-in peer.

### Native envelope (standalone server)

**Request** — one JSON message per connection:

```jsonc
{
  "agent": "optimus",                  // which agent (must exist in the registry)
  "task": "Fix the failing check.",    // the task text
  "constraints": {
    "timeout_s": 120,                  // per-command wall-clock in the Cell
    "max_iterations": 30,              // overrides the profile ceiling
    "network": false                   // outbound network in the Cell (default: off)
  },
  "repo_path": "/abs/path/to/project", // Cell workspace + Graphify root (optional)
  "job_id": "…"                        // optional; generated if omitted
}
```

A malformed request (e.g. missing `agent`) is rejected with a single `error`
event and a socket close (code 1003).

**Response** — a stream of events until a terminal frame:

```jsonc
{"job_id":"…","type":"started",     "data":{"agent":"optimus","job_id":"…"}}
{"job_id":"…","type":"chunk",       "data":"assistant text delta"}
{"job_id":"…","type":"tool",        "data":{"id":"…","name":"run_command","input":{…}}}
{"job_id":"…","type":"tool_result", "data":{"tool_use_id":"…","is_error":false,"content":"…"}}
{"job_id":"…","type":"done",        "data":"final summary"}      // terminal
{"job_id":"…","type":"error",       "data":"verbatim error text"} // terminal
```

The `type` values reuse Mark VI's streaming vocabulary
(`chunk` / `tool` / `tool_result` / `done` / `error`) verbatim.

### Mark VI frame mapping

The peer front door translates both directions:

| Mark VI frame | Native mapping | Result |
|---------------|----------------|--------|
| `task_dispatch {task_id, from, task, cwd}` | `JobRequest` | fire-and-await → one `task_result {task_id, result, status}` |
| `chat_request {chat_id, history, cwd, …}` | `JobRequest` (task = last user turn) | streamed → `chat_event` frames until terminal |
| `chat_cancel {chat_id}` | interrupt signal | aborts that run cleanly |
| `shutdown` | stop | disconnect, no reconnect |

Because a `JobEvent`'s `type` already matches the inner `chat_event` vocabulary,
Mark VI re-wraps each frame with no translation.

---

## 9. Agents — the rebrandable core

The Warden engine contains **no** `"optimus"` or `"centurion"` string — a property
enforced by a test. An agent is **two files in a folder**, mirroring the
S.P.E.D.A. backend's own fork contract (a profile module plus a prompt directory):

```
forge/agents/optimus/
├── profile.toml        # identity 1 — model, tool allowlist, Cell policy, mode
└── system_prompt.md    # identity 2 — the system prompt
```

```toml
# profile.toml
agent_id       = "optimus"
name           = "Optimus"
domain         = "systems, code & infrastructure"
model          = "claude-sonnet-4-6"   # model IDs live only in profiles
tools          = ["coding"]            # the tool allowlist is the security boundary
permission_mode = "act"                # "act" | "plan"
max_iterations = 30

[cell]
allow_network = false
cpus          = 2.0
memory_mb     = 2048
timeout_s     = 120
```

### Adding Centurion (or any third agent)

1. Create `forge/agents/centurion/` with its own `profile.toml` (its model, its
   security-tooling allowlist, its Cell policy — e.g. `allow_network = true` for
   scans) and `system_prompt.md`.
2. Register a Centurion toolset under `forge/tools/`.
3. Run it: `python -m forge connect --agent centurion`.

No engine change. If adding an agent required editing the Warden, the
parameterization would have failed.

---

## 10. Configuration reference

All configuration is environment-driven; `.env.example` documents every variable.
Values that are required only when used are validated at their point of use and
**fail loud** — the Forge assumes its environment rather than degrading silently.

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | — | Reasoning model credential. Not needed for `demo`. |
| `SPEDA_API_KEY` | — | Authenticates the peer's WebSocket handshake to Mark VI. |
| `SPEDA_WS_URL` | `ws://127.0.0.1:8000/agents/ws/optimus` | Mark VI agents endpoint. |
| `FORGE_CELL_BACKEND` | `auto` | `docker` \| `subprocess` \| `auto`. |
| `FORGE_WORKSPACE_ROOT` | `./.forge/workspaces` | Root for per-agent workspaces. |
| `FORGE_CELL_IMAGE` | `python:3.12-slim` | Image for `DockerCell`. |
| `FORGE_GRAPHIFY_BIN` | *(PATH)* | Override for the `graphify` executable. |
| `FORGE_AGENT` | `optimus` | Which agent `connect` runs as. |
| `FORGE_LOG_LEVEL` | `INFO` | Standard logging level. |

### Commands

```bash
python -m forge demo                      # offline end-to-end demo (no key)
python -m forge serve --host H --port P   # standalone native-contract job server
python -m forge connect --agent optimus   # connect to Mark VI as a peer
python -m forge agents                    # list configured agents and toolsets
python -m pytest                          # the test suite
```

---

## 11. Connecting to S.P.E.D.A. Mark VI

Mark VI owns the peer's lifecycle. Its launcher starts the peer as a child
process, injecting the environment it needs; the peer connects back to
`WS /agents/ws/<agent_id>`, authenticates with `X-API-Key: SPEDA_API_KEY`, sends
`agent_register`, and then serves `task_dispatch` / `chat_request` frames with
`task_result` / `chat_event` streams.

```bash
# On the Forge host, as the agent Mark VI expects:
SPEDA_API_KEY=…  SPEDA_WS_URL=ws://mark-vi-host:8000/agents/ws/optimus \
python -m forge connect --agent optimus
```

**Graceful fallback is a system-wide contract.** The Forge is never a hard
dependency: when the peer is offline, Mark VI's `external_backend` profile detects
the missing socket and answers in-process instead. The peer, for its part,
reconnects with capped exponential backoff and, on disconnect, aborts in-flight
runs cleanly so nothing streams into a dead socket.

> **Compatibility note.** Mark VI's current launcher invokes `python -m
> optimus.peer` (the Mark I entry point). To drive Mark II, point its peer
> directory / launch command at `python -m forge connect --agent optimus`, or add
> a one-line shim module. The wire protocol is unchanged, so no backend code
> changes are required.

---

## 12. Development

```bash
pip install -e ".[dev]"     # pytest
python -m pytest            # 13 tests
python -m forge demo        # full-chain smoke test
```

The test suite covers the load-bearing patterns directly: the loop's single stop
condition, the iteration ceiling, clean interruption, errors-as-results for
unknown tools and bad input, the bypass-immune safety gate, read-before-write
freshness, wire-contract validation, and the no-identity-in-core invariant. Async
tests wrap `asyncio.run` themselves, so no `pytest-asyncio` plugin is required.

---

## 13. Design principles

The Forge is built by asking one question of every line: **does this serve a user,
environment, model, repository, or policy that cannot occur in this single
deployment?** If yes, it is product scaffolding and is omitted.

**Adopted** — the irreducible mechanics of an agent:

- One `while True` loop, one state object, one typed exit with an enumerated
  reason.
- The model stopping tool requests as the sole loop-exit signal.
- A single iteration ceiling; cooperative interruption checked at both boundaries.
- The name + description + schema tool contract with fail-closed defaults.
- A fixed validate → permit → execute pipeline where every failure is a
  correctable result, never an escaping exception.
- Read-before-write freshness and per-result size caps.
- A short permission chain ending in a bypass-immune irreversibility gate.

**Deliberately omitted** — audience-only overhead:

- Telemetry and analytics event logging.
- Feature-flag / A-B rollout infrastructure.
- Multiple simultaneous entrypoint modes.
- Model-fallback escalation ladders and layered recovery state machines.
- Permission-prompt UI systems, denial-tracking, and multi-source rule layering.
- A speculative context-compaction cascade.

One environment, one mode, fail loud — instead of degrading gracefully through
ladders built for conditions this deployment cannot reach.

---

## 14. Project layout

```
forge/
├── __main__.py          CLI: demo | serve | connect | agents
├── config.py            flat, environment-driven settings
├── demo.py              offline end-to-end proof
├── agents/              rebrandable core — one folder per agent (two files each)
│   ├── config.py        AgentConfig — the injected identity
│   ├── registry.py      generic loader; contains no agent names
│   └── optimus/         profile.toml + system_prompt.md
├── warden/              the loop engine — contains no identity strings
│   ├── engine.py        the while-true loop; interruption at both boundaries
│   ├── state.py         LoopState + Terminal{reason}
│   ├── tool.py          the fail-closed tool contract
│   ├── dispatch.py      validate→permit→execute; errors-as-results; size caps
│   ├── permissions.py   precedence chain + bypass-immune safety gate
│   └── filestate.py     read-before-write freshness
├── tools/               curated toolset: shell, files, graph
├── graph/sidecar.py     Graphify MCP stdio client
├── cell/                the sandbox contract + Docker / subprocess backends
├── model/               the Model protocol + Anthropic + deterministic stand-in
└── gate/                protocol (native + Mark VI mapping), runner, server, peer
tests/test_forge.py      13 tests over the load-bearing patterns
```

---

<div align="center">

**The Warden reasons. The Cell executes. Neither is collapsed for convenience.**

</div>
