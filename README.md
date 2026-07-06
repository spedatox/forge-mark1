# The Forge (Optimus Mark II / Centurion execution peer)

A standalone, single-operator **execution peer** for **S.P.E.D.A. Mark VI**. The
Forge hosts the two agents that need privileged execution — running shell
commands, generated code, and security tooling — deliberately kept out of the
main backend so it doesn't inherit their threat model.

* **Optimus** — a coding agent (built in this pass).
* **Centurion** — a security agent (addable later as a second config; not built here).

Both are the **same engine** with a different identity, toolset, and sandbox.
There is one loop, one state object, one typed exit — and the identity that turns
that engine into "Optimus" lives entirely in two external files.

This is **Mark II**. Mark I failed by porting Claude Code 1:1 and inheriting an
apparatus built for millions of untrusted users — telemetry, feature flags, five
permission modes, model-fallback ladders, recovery state machines. Mark II studies
the same source *for shape*, adopts only what is load-bearing for a single trusted
operator, and refuses the rest on principle. The pattern decisions are grounded in
an internal architecture study (`CLAUDE_CODE_ARCHITECTURE_STUDY.md`); section
references (§) throughout the code point back to the build brief.

---

## 1. Architecture at a glance

Three layers, never collapsed (§9.3 — *Warden reasons, Cell executes*):

```
                    ┌─────────────────────────────────────────────┐
   Mark VI  ──WS──► │  GATE      network boundary + wire contract  │
   (or standalone)  │            validates jobs, streams results   │
                    └───────────────┬─────────────────────────────┘
                                    │ run_job(request)
                    ┌───────────────▼─────────────────────────────┐
                    │  WARDEN    the parameterized loop engine     │
                    │            act → observe → evaluate → adapt   │
                    │            ZERO identity strings (§2)         │
                    │   ┌──────────────┐        ┌────────────────┐  │
                    │   │ Graphify     │        │ Tool boundary  │  │
                    │   │ sidecar (MCP)│        │ validate→permit│  │
                    │   │ read-side ctx│        │ →execute (§4)  │  │
                    │   └──────────────┘        └───────┬────────┘  │
                    └───────────────────────────────────┼──────────┘
                                                         │ Cell.run/read/write/reset
                                    ┌────────────────────▼─────────┐
                                    │  CELL   one isolated sandbox  │
                                    │         per agent, never      │
                                    │         shared (§9.1)         │
                                    └───────────────────────────────┘
```

| Layer | Module | Responsibility |
|-------|--------|----------------|
| **Gate** | `forge/gate/` | Accept a job (native envelope **or** Mark VI frames), validate, stream `JobEvent`s. Two front doors — `server.py` (standalone) and `peer.py` (Mark VI). |
| **Warden** | `forge/warden/` | The `while True` loop, the tool boundary, permissions. Injected with an `AgentConfig`; contains no agent names. |
| **Cell** | `forge/cell/` | `run / write / read / reset` inside an isolated sandbox. Docker (prod) or subprocess (dev). |
| **Graphify** | `forge/graph/` | A Warden-side MCP sidecar giving the agent a queryable codebase graph (§5). |
| **Agents** | `forge/agents/` | The rebrandable-core boundary: one folder per agent, two files each. |

### Technical decisions (and why)

* **Language: Python 3.11+.** Mark VI is Python/FastAPI, the peer must speak its
  WebSocket protocol, Graphify is Python, and the study's tech-mapping targets
  Python (Pydantic for Zod schemas, `asyncio.Event` for `AbortController`). One
  language across the whole network.
* **Schemas: Pydantic.** The study's single best pattern is the Zod tool contract
  validated before dispatch. Pydantic `model_json_schema()` is the model-facing
  schema; `model_validate()` is the validate stage — a public, well-shaped
  standard reused exactly (§4).
* **No borrowed product scaffolding (§9.5).** No telemetry, no feature flags, no
  multi-entrypoint dispatch, no model-fallback ladder, no recovery state machine,
  no permission-prompt UI, no multi-tenant config surface. One environment, one
  mode, **fail loud**.

---

## 2. The Cell — isolation choice and rationale (§8)

**Contract (identical for every backend):**

```python
Cell.run(command, timeout, env) -> CommandResult{stdout, stderr, exit_code, timed_out}
Cell.write(path, content)
Cell.read(path)
Cell.reset()
```

Default posture: **no outbound network** unless the job asks; CPU / memory / PIDs /
wall-clock **capped**; output **capped**; runs **non-root** (§8).

Two backends implement the contract:

### `DockerCell` — the production default
One throwaway container per agent, started once (`sleep infinity`) with commands
via `docker exec` so filesystem and installed-package state persist like a real
machine. Flags: `--network none` (unless network is granted), `--memory`,
`--cpus`, `--pids-limit`, `--user 1000:1000`, `--cap-drop ALL`,
`--security-opt no-new-privileges`, and a single bind mount of the job's repo to
`/workspace`. `reset()` destroys and recreates the container.

**Why Docker.** It is exactly the technology **Mark VI already sandboxes with**
(`packages/sandbox` is a stdlib exec server in an isolated, resource-limited,
secret-free container) — so the two systems share one mental model. It gives a
real kernel-namespace + cgroup boundary with resource caps as one well-understood
dependency. A microVM (Firecracker/gVisor) would be a stronger boundary but adds a
KVM/Linux requirement the single-operator Forge does not need for its threat
model. That trade-off is deliberate and documented here rather than hidden.

### `SubprocessCell` — reduced-isolation fallback (dev / CI / this Windows box)
Same four-method contract, but the boundary is a per-agent **workspace jail** plus
wall-clock and output caps, not a kernel namespace. It strips provider/API secrets
from the command environment and best-effort black-holes network via proxy env
vars. It is **honest about what it is**: a workspace jail, *not* a security
boundary against hostile code. The factory (`FORGE_CELL_BACKEND=auto`) picks
Docker when the daemon answers and this otherwise — which is why the offline demo
runs here without Docker installed.

**Two Cells, never one shared, no exceptions (§9.1).** The Gate builds a fresh
Cell per job via the factory; container names / workspace dirs are per-agent, so
Optimus and Centurion can never touch the same sandbox.

---

## 3. Wire contract (§7)

The exact schema was ours to design. It is designed to (a) stand alone and (b)
map 1:1 onto Mark VI's **existing** agents-WebSocket protocol so the Forge is a
drop-in peer.

### Native envelope (standalone `server.py`)

**Request** — one JSON message per connection:

```jsonc
{
  "agent": "optimus",                 // which agent (must exist in the registry)
  "task": "Fix the failing check.",   // the task
  "constraints": {                    // execution constraints (§7)
    "timeout_s": 120,                 // per-command wall-clock in the Cell
    "max_iterations": 30,             // overrides the profile's ceiling
    "network": false                  // outbound network in the Cell (default: off)
  },
  "repo_path": "/abs/path/to/project",// Cell workspace + Graphify root (optional)
  "job_id": "…"                       // optional; generated if absent
}
```

A **malformed** request (e.g. missing `agent`) is rejected with a single `error`
event and the socket closes with code 1003.

**Streamed results** — many `JobEvent`s until terminal (results stream; a client
observes progress, never just a final blob — §7):

```jsonc
{"job_id":"…","type":"started",    "data":{"agent":"optimus","job_id":"…"}}
{"job_id":"…","type":"chunk",      "data":"assistant text delta"}
{"job_id":"…","type":"tool",       "data":{"id":"…","name":"run_command","input":{…}}}
{"job_id":"…","type":"tool_result","data":{"tool_use_id":"…","is_error":false,"content":"…"}}
{"job_id":"…","type":"done",       "data":"final summary"}      // terminal
{"job_id":"…","type":"error",      "data":"real error text"}    // terminal (fail loud)
```

`type` deliberately reuses Mark VI's SSE/`chat_event` vocabulary
(`chunk`/`tool`/`tool_result`/`done`/`error`).

### Mark VI mapping (`peer.py`)

The Gate also speaks Mark VI's protocol directly (`forge/gate/protocol.py` maps
both ways):

| Mark VI frame | → | Native | Result |
|---|---|---|---|
| `task_dispatch` `{task_id, from, task, cwd}` | `JobRequest` | fire-and-await → one `task_result` `{task_id, result, status}` |
| `chat_request` `{chat_id, history, cwd, …}` | `JobRequest` (task = last user turn) | streamed → `chat_event` frames `{type, data}` until terminal |
| `chat_cancel` `{chat_id}` | interrupt | aborts that run cleanly |
| `shutdown` | stop | disconnect, no reconnect |

Because a `JobEvent`'s `type` already matches `chat_event`'s inner vocabulary,
Mark VI's `ExternalAgentProxy` re-wraps each frame 1:1 with no translation.

---

## 4. Rebrandable core — adding an agent (§2)

The Warden engine contains **no** `"optimus"` / `"centurion"` string (enforced by
`tests/test_forge.py::test_engine_core_has_no_agent_identity_strings`). An agent
is **two files in a folder**, mirroring Mark VI's own fork contract
(`app/profiles/<id>.py` + `app/prompts/agents/<id>/`):

```
forge/agents/optimus/
├── profile.toml         # identity 1: agent_id, model, tool allowlist, Cell policy
└── system_prompt.md     # identity 2: the system prompt
```

`profile.toml`:

```toml
agent_id = "optimus"
name = "Optimus"
domain = "systems, code & infrastructure"
model = "claude-sonnet-4-6"     # model IDs live only here (Mark VI Rule 10)
tools = ["coding"]              # the tool allowlist IS the security boundary
permission_mode = "act"         # "act" | "plan"
max_iterations = 30
[cell]
allow_network = false
cpus = 2.0
memory_mb = 2048
timeout_s = 120
```

**To add Centurion later:** create `forge/agents/centurion/` with its own
`profile.toml` (its model, its security-tooling allowlist, its Cell policy —
e.g. `allow_network = true` for scans) and `system_prompt.md`. Register a
Centurion toolset in `forge/tools/`. Then `python -m forge connect --agent
centurion`. **No engine edit.** If adding one required touching the Warden, the
parameterization would have failed.

---

## 5. Codebase understanding — Graphify (§5)

The Forge does **not** build a custom indexer. It uses **Graphify** (MIT, PyPI
`graphifyy`) directly as a dependency, wired in as a **Warden-side capability**
(read-side context the agent queries — never a Cell operation):

1. **Index once per session:** `python -m graphify <repo> --no-label` →
   `graphify-out/graph.json` (pure tree-sitter AST extraction; the build env has
   provider keys stripped so it never makes an LLM call).
2. **Serve:** `python -m graphify.serve graph.json --transport stdio` — an MCP
   sidecar the Warden talks to (`forge/graph/sidecar.py` is a ~120-line MCP stdio
   client: `initialize` → `notifications/initialized` → `tools/call`).
3. **Query:** three tools — `graph_query` (semantic/BFS search), `graph_path`
   (shortest path between entities), `graph_overview` (stats + "god nodes").

This is what lets the agent ask *"where is `add` defined and who calls it?"*
instead of re-reading whole files — directly reducing reliance on aggressive
context compaction (§5). If indexing or the handshake fails, the graph tools
return an `is_error` result telling the model to fall back to `read_file`; a
missing graph never crashes a job. Context management is therefore intentionally
minimal (§5): recent messages plus per-result size caps, with no compaction
cascade built speculatively.

---

## 6. Permission and safety (§6)

One short precedence chain, ending in a **bypass-immune** safety gate:

```
1. session allow-list         operator's known-safe repeats → allow
2. tool.check_permissions     the tool's own opinion
3. SAFETY GATE (bypass-immune) VCS internals, credentials, shell config,
                               destructive-marked ops → DENY even if 1–2 allow
4. mode                       plan → deny mutations; act → allow
```

* **One active mode** (`act`) with the gate always on, plus an optional read-only
  `plan` mode for reviewing intended actions. No LLM risk classifier, no
  denial-tracking, no policy/project/enterprise rule layering — **the operator is
  the risk assessor**, and there is no second party to govern.
* The gate is tool-agnostic: it inspects a tool's `is_destructive` flag and any
  `path` / `command` argument against protected-path and high-blast-radius
  patterns (`.git/`, `.ssh`, `.env`, credentials, shell rc files; `rm -rf`,
  `git push --force`, `curl … | sh`, `dd`, fork bombs, `sudo`, …). A gated
  operation requires an explicit operator allow-list entry.

### Tool boundary (§4)

A tool the model sees is exactly `name` + `description` + input schema. Everything
else — `is_read_only`, `is_concurrency_safe`, `is_destructive`,
`max_result_chars`, permissions — is harness-side and **fail-closed** (assume not
safe, not read-only, not auto-permitted). The dispatch pipeline is a fixed
`validate → permit → execute` gauntlet where **every** failure (unknown tool, bad
input, permission denial, execution crash) becomes an identical `is_error`
`tool_result` fed back to the model — never an exception that escapes the loop.
Oversized results spill to a file in the Cell and are replaced with a preview +
path.

---

## 7. Running the Forge

### Install

```bash
pip install -e .            # core: pydantic, websockets, anthropic
pip install graphifyy       # the Graphify dependency (§5)
# Docker optional — without it the Cell auto-selects the subprocess backend.
```

### Offline demo — proves the whole chain, no API key (§10)

```bash
python -m forge demo
```

Creates a tiny repo with a buggy `add()`, then Optimus's Warden: maps the codebase
**via the Graphify graph**, runs the check (observes the failure), reads the file,
fixes it, re-runs the check (observes the pass), and finishes — with tool events
**streaming** as they happen and the fix verified on disk. Uses the deterministic
`ScriptedModel` whose steps branch on the running transcript (a real
observe/adapt), so it needs no key and no network.

### Standalone job server (native wire contract)

```bash
python -m forge serve --host 127.0.0.1 --port 8770
# then send a JobRequest JSON over the socket (see §3). Needs ANTHROPIC_API_KEY
# for the real model; the demo path injects the ScriptedModel instead.
```

### Other commands

```bash
python -m forge agents      # list configured agents and their toolsets
python -m forge connect --agent optimus   # connect to Mark VI as a peer (§8 below)
python -m pytest            # 13 tests covering the load-bearing patterns
```

Configuration is via environment (`.env.example` documents every variable):
`ANTHROPIC_API_KEY`, `SPEDA_API_KEY`, `SPEDA_WS_URL`, `FORGE_CELL_BACKEND`,
`FORGE_WORKSPACE_ROOT`, `FORGE_CELL_IMAGE`.

---

## 8. How Mark VI connects (§9.2 graceful fallback)

Mark VI already knows how to drive this peer. Its `optimus_peer.py` launcher
starts the peer as a child process and injects the env it needs
(`SPEDA_API_KEY`, `SPEDA_WS_URL`, workspace); the peer then connects back to
`WS /agents/ws/<agent_id>`, authenticates with `X-API-Key: SPEDA_API_KEY`, and
sends `agent_register`. From there Mark VI dispatches `task_dispatch` /
`chat_request` frames and the Forge answers with `task_result` / `chat_event`
streams (§3).

```bash
# On the Forge host, as the agent Mark VI expects:
SPEDA_API_KEY=…  SPEDA_WS_URL=ws://mark-vi-host:8000/agents/ws/optimus \
python -m forge connect --agent optimus
```

**Graceful fallback is a Forge-wide contract (§9.2).** The Forge is never a hard
dependency: when this peer is offline, Mark VI's `external_backend` profile
detects the missing WebSocket and answers in-process instead. The peer itself
reconnects with capped exponential backoff and, on disconnect, aborts in-flight
runs cleanly so nothing streams into a dead socket.

> Compatibility note: Mark VI's current launcher shells out to
> `python -m optimus.peer` (the Mark I path). For Mark II, point its
> `optimus_peer_dir` / launch command at `python -m forge connect --agent optimus`,
> or add a one-line shim module. The wire protocol is unchanged, so no backend
> code changes are required.

---

## 9. What this build delivers (§10)

- ✅ Gate accepts a connection and a well-formed job; rejects malformed ones
  (`error` + close). *Verified live and in tests.*
- ✅ Optimus's Warden runs a real act→observe→adapt loop against a trivial task
  using a real Cell. *`python -m forge demo`.*
- ✅ Graphify is wired in and actually queried during the loop (`graph_overview`
  + `graph_query` return real graph data) rather than re-reading files.
- ✅ Results stream — intermediate `tool` / `tool_result` events are observable
  before the final `done`.
- ✅ This README documents the Cell choice, the wire schema, standalone run, and
  the Mark VI connection.

**Not built this pass (by design):** Centurion's Warden. It is a second
configuration of the same engine — a new `forge/agents/centurion/` folder and a
security toolset — touching no core code (§4, §10).

---

## 10. Layout

```
forge/
├── __main__.py          CLI: demo | serve | connect | agents
├── config.py            flat env-driven settings
├── demo.py              offline end-to-end proof
├── agents/              rebrandable core — one folder per agent (two files each)
│   ├── config.py        AgentConfig (the injected identity)
│   ├── registry.py      generic loader; no agent names
│   └── optimus/         profile.toml + system_prompt.md
├── warden/              the loop engine — ZERO identity strings
│   ├── engine.py        one while-true loop, abort at both boundaries
│   ├── state.py         LoopState + Terminal{reason}
│   ├── tool.py          the tool contract (fail-closed)
│   ├── dispatch.py      validate→permit→execute, errors-as-results, size cap
│   ├── permissions.py   precedence chain + bypass-immune safety gate
│   └── filestate.py     read-before-write freshness
├── tools/               curated toolset: shell, files, graph
├── graph/sidecar.py     Graphify MCP stdio client
├── cell/                Cell contract + Docker / subprocess backends
├── model/               Model protocol + Anthropic + ScriptedModel
└── gate/                protocol (native + Mark VI mapping), runner, server, peer
tests/test_forge.py      13 tests over the load-bearing patterns
```
