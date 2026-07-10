<div align="center">

# F.O.R.G.E.

**F**ramework of **O**perational **R**untime & **G**ated **E**xecution

*A standalone, single-operator execution peer for S.P.E.D.A. Mark VI.*

Privileged execution — shell, generated code, and security tooling — isolated in
its own process so the main backend never inherits its threat model.

[![python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![tests](https://img.shields.io/badge/tests-13%20passing-brightgreen.svg)](tests/test_forge.py)
[![isolation](https://img.shields.io/badge/cell-docker%20%7C%20subprocess-informational.svg)](#5-the-cell--sandbox-isolation)
[![status](https://img.shields.io/badge/build-Mark%20II-success.svg)](#)

</div>

## Overview

Forge Mark I is the execution backend for agents that require privileged access — shell commands, generated code execution, and security tooling — isolated in its own process so the main S.P.E.D.A. backend never inherits its threat model.

It hosts the following agents:

| Agent | Domain | Status |
|---|---|---|
| **Optimus** | Coding — writes, runs, and iterates on code | ✅ Active |
| **Centurion** | Security — runs scans, analyzes results, adapts | 📋 Configurable |

Both agents run on the same parameterized engine. Adding a new agent requires only a configuration folder — no engine modifications.

### Key Features

- **Act → Observe → Evaluate → Adapt** execution loop with typed termination
- **Sandbox isolation** via Docker containers or subprocess jails
- **Multi-provider LLM routing** across 6 providers with optional fallback chains
- **Codebase understanding** via Graphify knowledge graph (MCP sidecar)
- **Bypass-immune safety gate** for irreversible operations
- **Native wire protocol** with 1:1 Mark VI frame mapping
- **Live streaming** of all tool activity and reasoning
- **Model override from Heartbreaker** — the UI's model picker controls which LLM the Forge uses per-turn

---

## Table of Contents

- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Execution Loop](#execution-loop)
- [Sandbox Isolation](#sandbox-isolation)
- [Codebase Understanding](#codebase-understanding)
- [Security Model](#security-model)
- [Wire Protocol](#wire-protocol)
- [Multi-Provider LLM Routing](#multi-provider-llm-routing)
- [Agent Configuration](#agent-configuration)
- [Configuration Reference](#configuration-reference)
- [Connecting to S.P.E.D.A. Mark VI](#connecting-to-speda-mark-vi)
- [Testing](#testing)
- [Project Structure](#project-structure)

---

## Quick Start

### Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Core runtime |
| Docker (optional) | Production sandbox; falls back to subprocess isolation |
| LLM API key | At least one provider key for live operation |

### Installation

```bash
pip install -e .
pip install graphifyy          # optional: codebase graph capability
```

### Run the Demo (Offline)

No API key or network access required:

```bash
python -m forge demo
```

The demo provisions a repository with a deliberate bug, drives the full execution loop against it — mapping the codebase, running the failing test, reading source, applying a fix, verifying — all with live streaming. Uses a deterministic model stand-in.

**Expected output:**

```
=== TERMINAL ===
reason: completed
iterations: 7
final: Done. The bug in `add` was a subtraction instead of an addition; ...

fix present on disk: True   loop completed: True
=== DEMO PASSED ===
```

### Run Live

```bash
# Standalone server
python -m forge serve --host 127.0.0.1 --port 8770

# Connect to S.P.E.D.A. Mark VI as a peer
python -m forge connect --agent optimus
```

---

## Architecture

Forge is built on three layers with a strict separation: **the Warden reasons, the Cell executes**.

```
                     ┌──────────────────────────────────────────────┐
   S.P.E.D.A.        │  GATE                                        │
   Mark VI  ──WS──►  │  Network boundary. Validates job requests,    │
   (or native        │  streams results. Native contract and Mark VI │
    client)          │  frame mapping.                               │
                     └───────────────────────┬──────────────────────┘
                                             │ run_job(request) → stream
                     ┌───────────────────────▼──────────────────────┐
                     │  WARDEN                                       │
                     │  Parameterized loop engine. One while-true    │
                     │  loop, one state object, one typed exit.      │
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
|---|---|---|
| **Gate** | `forge/gate/` | Accepts jobs, validates requests, streams `JobEvent`s. Front doors: `server.py` (standalone) and `peer.py` (Mark VI) |
| **Warden** | `forge/warden/` | Execution loop, tool boundary, permission engine. Injected with `AgentConfig`; contains no identity strings |
| **Cell** | `forge/cell/` | Sandbox contract (`run` / `write` / `read` / `reset`) with Docker and subprocess backends |
| **Graphify** | `forge/graph/` | Warden-side MCP sidecar exposing a queryable codebase knowledge graph |
| **Agents** | `forge/agents/` | One folder per agent, two files each (`profile.toml` + `system_prompt.md`) |
| **Model** | `forge/model/` | Multi-provider reasoning client with deterministic stand-in for testing |

---

## Execution Loop

The Warden runs a single `while True` loop over one mutable state object, returning exactly one typed result:

```python
Terminal(reason: StopReason, final_text, iterations, error, messages)

class StopReason(Enum):
    COMPLETED       # model stopped requesting tools — the only success path
    ABORTED         # interrupt signal fired at a boundary
    MAX_ITERATIONS  # iteration ceiling reached
    ERROR           # unrecoverable failure, surfaced verbatim
```

### Guarantees

| Guarantee | Description |
|---|---|
| **Single stop condition** | A turn with no tool requests ends the loop |
| **Single iteration ceiling** | One `max_iterations` guard (default: 30) |
| **Clean interruption** | `asyncio.Event` checked at both boundaries — after model response and after tool execution |
| **Safe concurrency** | Concurrency-safe tools run in parallel; all others serialize |
| **Errors as data** | Unknown tools, bad input, permission denials, and exceptions all become correctable `is_error` tool results |
| **Result size caps** | Oversized results spill to file with a preview, preventing context window overflow |

### Tool Contract

A tool exposes three things to the model: **name**, **description**, and **input schema** (Pydantic → JSON Schema). All metadata — read-only, concurrency-safe, destructive flags — is harness-side and invisible to the model.

Defaults are **fail-closed**: new tools are assumed not read-only, not concurrency-safe, not destructive, and not automatically permitted unless explicitly declared.

Dispatch follows a fixed pipeline: **validate → permit → execute**.

---

## Sandbox Isolation

Every backend implements one contract:

```python
Cell.run(command, timeout, env) -> CommandResult(stdout, stderr, exit_code, timed_out)
Cell.write(path, content)
Cell.read(path)
Cell.reset()
```

**Default posture:** no outbound network, CPU/memory/PID capped, output byte-capped, non-root execution. One Cell per agent, never shared.

### Docker Cell (Production)

One throwaway container per agent, started once with commands executed via `docker exec`:

| Control | Flag |
|---|---|
| No network | `--network none` |
| Memory ceiling | `--memory <mb>` |
| CPU ceiling | `--cpus <n>` |
| Fork-bomb guard | `--pids-limit <n>` |
| Non-root | `--user 1000:1000` |
| Drop capabilities | `--cap-drop ALL` |
| No privilege escalation | `--security-opt no-new-privileges` |
| Workspace | Single bind mount to `/workspace` |

### Subprocess Cell (Fallback)

Same four-method contract with a per-agent workspace jail and wall-clock/output caps. Strips API secrets from the environment. Selected automatically when Docker is unavailable.

Backend selection: `FORGE_CELL_BACKEND` (`docker` | `subprocess` | `auto`)

---

## Codebase Understanding

Forge integrates [Graphify](https://github.com/Graphify-Labs/graphify) (MIT, PyPI: `graphifyy`) as a Warden-side MCP sidecar for codebase navigation.

```
1. Index    graphify <repo> --no-label → graph.json (tree-sitter AST, no LLM call)
2. Serve    graphify.serve graph.json --transport stdio
3. Query    forge/graph/sidecar.py speaks JSON-RPC to the sidecar
```

| Tool | Purpose |
|---|---|
| `graph_query` | Natural-language / keyword search across the graph |
| `graph_path` | Shortest relationship path between named entities |
| `graph_overview` | Graph statistics and most-connected nodes |

If indexing or MCP handshake fails, graph tools return an `is_error` result instructing the model to fall back to reading files directly. A missing graph never fails a job.

---

## Security Model

A single precedence chain ending in a hard, non-bypassable gate:

```
1. Session allow-list        operator's known-safe repeats              → allow
2. Tool's own check          tool-specific opinion                      → allow / deny
3. SAFETY GATE (bypass-immune)  irreversible / high-blast-radius ops    → deny
4. Mode                      plan → deny mutations; act → allow
```

### Safety Gate

The safety gate fires regardless of any allow-list entry or mode:

| Protected Paths | Dangerous Commands |
|---|---|
| `.git/` internals, `.ssh`, `.aws`, `.kube` | `rm -rf`, `git push --force`, `git reset --hard` |
| `.env`, credentials, `*.pem`, `*.key`, `.netrc` | `curl … \| sh`, `wget … \| sh` |
| Shell rc/profile files | `mkfs`, `dd`, `sudo`, `shutdown`, fork bombs |

---

## Wire Protocol

### Native Envelope

**Request** (one JSON message per WebSocket connection):

```json
{
  "agent": "optimus",
  "task": "Fix the failing check.",
  "constraints": {
    "timeout_s": 120,
    "max_iterations": 30,
    "network": false
  },
  "repo_path": "/path/to/project",
  "model_override": "gemini:gemini-2.5-flash",
  "job_id": "..."
}
```

**Response** (streamed events until terminal):

```json
{"job_id":"…","type":"started",     "data":{"agent":"optimus","job_id":"…"}}
{"job_id":"…","type":"chunk",       "data":"assistant text delta"}
{"job_id":"…","type":"tool",        "data":{"id":"…","name":"run_command","input":{…}}}
{"job_id":"…","type":"tool_result", "data":{"tool_use_id":"…","is_error":false,"content":"…"}}
{"job_id":"…","type":"done",        "data":"final summary"}
{"job_id":"…","type":"error",       "data":"error text"}
```

### Mark VI Frame Mapping

| Mark VI Frame | Forge Mapping |
|---|---|
| `task_dispatch {task_id, from, task, cwd}` | `JobRequest` → `task_result` |
| `chat_request {chat_id, history, model, cwd}` | `JobRequest` → streamed `chat_event` frames |
| `chat_cancel {chat_id}` | Abort signal |
| `shutdown` | Disconnect |

The `model` field in `chat_request` carries Heartbreaker's model picker selection. Forge uses it as a per-turn override of the agent profile's default model. When `null`, the profile default applies.

---

## Multi-Provider LLM Routing

The Forge holds its own credentials and makes its own inference calls. Model selection uses `provider:model` refs:

| Provider | Example | Credential |
|---|---|---|
| Anthropic | `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai:gpt-5.1` | `OPENAI_API_KEY` |
| Gemini | `gemini:gemini-2.5-flash` | `GEMINI_API_KEY` |
| z.ai (GLM) | `zai:glm-4.6` | `ZAI_API_KEY` |
| DeepSeek | `deepseek:deepseek-v4-pro` | `DEEPSEEK_API_KEY` |
| Ollama | `ollama:llama3.1:8b` | `OLLAMA_BASE_URL` |

Bare refs route to Anthropic. All other providers use a shared OpenAI-compatible adapter with provider-specific handling at the wire boundary.

An optional fallback chain (`FORGE_LLM_FALLBACK_CHAIN`) provides operator-configured provider redundancy — tried in order only when opening a stream. Off by default.

### Model Selection Priority

```
1. Per-turn override (Heartbreaker model picker / API request)
2. Agent profile default (profile.toml)
```

---

## Agent Configuration

An agent is two files in a folder:

```
forge/agents/optimus/
├── profile.toml        # model, tools, Cell policy, permissions
└── system_prompt.md    # system prompt
```

### Profile Example

```toml
agent_id        = "optimus"
name            = "Optimus"
domain          = "systems, code & infrastructure"
model           = "claude-sonnet-4-6"
tools           = ["coding"]
permission_mode = "act"
max_iterations  = 30

[cell]
allow_network = false
cpus          = 2.0
memory_mb     = 2048
timeout_s     = 120
```

### Adding a New Agent

1. Create `forge/agents/<name>/` with `profile.toml` and `system_prompt.md`
2. Register a toolset under `forge/tools/` (if needed)
3. Run: `python -m forge connect --agent <name>`

No engine modifications required.

---

## Configuration Reference

All configuration is environment-driven. See [`.env.example`](.env.example) for the complete annotated list.

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Anthropic provider (bare model refs) |
| `OPENAI_API_KEY` | — | OpenAI provider |
| `GEMINI_API_KEY` | — | Google Gemini provider |
| `ZAI_API_KEY` | — | z.ai / GLM provider |
| `DEEPSEEK_API_KEY` | — | DeepSeek provider |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Local Ollama endpoint |
| `FORGE_LLM_FALLBACK_CHAIN` | *(off)* | Comma-separated fallback `provider:model` refs |
| `SPEDA_API_KEY` | — | Peer WebSocket authentication to Mark VI |
| `SPEDA_WS_URL` | `ws://127.0.0.1:8000/agents/ws/optimus` | Mark VI agents endpoint |
| `FORGE_CELL_BACKEND` | `auto` | `docker` \| `subprocess` \| `auto` |
| `FORGE_WORKSPACE_ROOT` | `./.forge/workspaces` | Root for per-agent workspaces |
| `FORGE_CELL_IMAGE` | `python:3.12-slim` | Docker image for Cell containers |
| `FORGE_GRAPHIFY_BIN` | *(PATH)* | Override for the `graphify` executable |
| `FORGE_LOG_LEVEL` | `INFO` | Logging level |

### CLI Commands

```bash
python -m forge demo                      # Offline end-to-end demo
python -m forge serve --host H --port P   # Standalone WebSocket job server
python -m forge connect --agent optimus   # Connect to Mark VI as a peer
python -m forge agents                    # List configured agents
```

---

## Connecting to S.P.E.D.A. Mark VI

```bash
SPEDA_API_KEY=<key> SPEDA_WS_URL=ws://<host>:8000/agents/ws/optimus \
python -m forge connect --agent optimus
```

The peer authenticates via `X-API-Key`, sends `agent_register`, and serves `task_dispatch` / `chat_request` frames. When the peer is offline, Mark VI falls back to its in-process profile automatically.

Reconnection uses capped exponential backoff. In-flight runs abort cleanly on disconnect.

---

## Testing

```bash
pip install -e ".[dev]"
python -m pytest                          # 20 tests
python -m forge demo                      # Full-chain smoke test
```

The test suite covers: loop stop conditions, iteration ceiling, clean interruption, errors-as-results, safety gate bypass immunity, read-before-write freshness, wire contract validation, multi-provider model routing, and the no-identity-in-core invariant.

---

## Project Structure

```
forge/
├── __main__.py              CLI entry point
├── config.py                Environment-driven settings
├── demo.py                  Offline end-to-end proof
├── agents/                  Agent configurations
│   ├── config.py            AgentConfig dataclass
│   ├── registry.py          Generic loader (no agent names)
│   └── optimus/             profile.toml + system_prompt.md
├── warden/                  Loop engine (no identity strings)
│   ├── engine.py            While-true loop with interrupt boundaries
│   ├── state.py             LoopState + Terminal
│   ├── tool.py              Fail-closed tool contract
│   ├── dispatch.py          validate → permit → execute pipeline
│   ├── permissions.py       Precedence chain + safety gate
│   └── filestate.py         Read-before-write freshness
├── tools/                   Shell, file, and graph toolsets
├── graph/sidecar.py         Graphify MCP stdio client
├── cell/                    Sandbox contract + backends
│   ├── base.py              Cell protocol + CellPolicy
│   ├── docker_cell.py       Docker backend
│   ├── subprocess_cell.py   Subprocess fallback
│   └── factory.py           Backend selection
├── model/                   Multi-provider reasoning client
│   ├── base.py              Model protocol
│   ├── anthropic_model.py   Native Anthropic SDK
│   ├── providers.py         OpenAI-compatible adapter
│   ├── factory.py           Provider selection + fallback chain
│   └── scripted.py          Deterministic stand-in for tests
└── gate/                    Network boundary
    ├── protocol.py          Wire contract (native + Mark VI mapping)
    ├── runner.py             Job assembly + execution
    ├── server.py            Standalone WebSocket server
    └── peer.py              Mark VI peer connection
tests/
├── test_forge.py            Core engine tests
└── test_providers.py        Multi-provider routing tests
```

---

## License

Private project — not licensed for redistribution.

