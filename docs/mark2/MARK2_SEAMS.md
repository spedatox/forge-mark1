# Mark II — The Extension Seams (Mark III Readiness Contract)

**Rule:** Mark II may not add a capability by hard-wiring it into the engine.
Every capability lands behind a **named seam** — a typed protocol with a
registration point at assembly time — so that Mark III's plugin architecture
(MCP servers, hooks, operator-defined skills) populates existing seams instead
of forcing a rewrite. This is the same discipline that made Mark II possible
on Mark I's skeleton, made explicit and enforced.

**What this document is:** the closed list of seams, each with its Mark II
shape and its intended Mark III payload. Every workstream in the other four
docs cites which seam it lands on. A workstream that needs a seam not listed
here must amend this document first — that review step is the enforcement.

**What this document is not:** a plugin system. Mark II ships **zero** plugin
loading, zero manifest formats, zero dynamic discovery. It ships interfaces
whose *second* implementation is cheap. (The first proof: `AutoDenyOracle` /
`PeerOracle` in TRUST — one protocol, two implementations, chosen at
assembly. Every seam below follows that shape.)

---

## The three laws

1. **Dependency direction.** Core modules (`warden/`, `cell/`, `model/`,
   `gate/`) define protocols and never import implementations of other
   seams' extensions. Implementations import core, never each other.
2. **One assembly point.** All seam wiring happens where jobs are assembled —
   `run_job` (per-job seams) and peer/server `main()` (process-wide seams).
   Nothing self-registers via import side effects; a future plugin loader is
   *one more caller* of the same registration functions.
3. **Additive wire.** New peer frames and JobEvent types are always additive;
   both ends ignore unknown types (verified behavior on the Mark VI router
   and required behavior in `ForgePeer._dispatch`). A Mark III plugin can
   therefore introduce frames without version negotiation.

---

## Seam 1 — Tool provision (`forge/warden/toolsource.py`, new in Mark II)

**Today:** `ALL_TOOLS` is a static class dict; `run_job` instantiates by name
from the profile's allowlist. A plugin tool or MCP server has nowhere to
stand.

**Mark II shape:**

```python
class ToolProvider(Protocol):
    async def provide(self, cfg: AgentConfig, request: JobRequest) -> dict[str, Tool]:
        """Tools this source contributes for this job. Called at job assembly."""
    async def close(self) -> None: ...
```

- `BuiltinToolProvider` wraps today's `ALL_TOOLS` + allowlist filtering —
  behavior identical to Mark I.
- `run_job` builds `tools` by folding an **ordered provider list** (later
  providers may not shadow earlier names — collision is a loud startup
  error, not a silent override). The provider list is process-wide state
  assembled in `main()`; Mark II's list has exactly one entry.
- Every TOOLBELT workstream (W7–W12) registers through
  `BuiltinToolProvider`, not by editing a dict at import time.

**Mark III payload:** `MCPToolProvider` (one per configured server — speaks
MCP, wraps each remote tool in a `Tool` adapter whose `call` proxies the
protocol; harness flags conservatively fail-closed: not read-only, not
concurrency-safe unless the server's annotations say otherwise), and
plugin-contributed native tools. The engine, dispatch gauntlet, and
permission chain need **zero changes** — a proxied MCP tool is just a `Tool`.

---

## Seam 2 — Permission policy (`PermissionOracle`, TRUST W13)

**Already an interface in the Mark II design** — the canonical seam example.

**Mark II shape:** `resolve()` chain fixed in core; the *ask* path delegates
to the oracle protocol; two implementations (`AutoDenyOracle`, `PeerOracle`).

**Mark III payload:** policy plugins as **decision middleware** — an ordered
`list[PermissionMiddleware]` consulted between chain steps 2 and 3 (each
returns allow/deny/ask/pass). The bypass-immune gate stays terminal and
non-pluggable **by design**: no plugin may weaken it, only tighten. This
asymmetry (plugins can restrict, never loosen) is the security invariant of
the whole plugin story and is stated here so Mark III inherits it as law.

---

## Seam 3 — The dispatch gauntlet as named stages (`warden/dispatch.py`)

**Today:** `dispatch_tool` is one function with five numbered comments
(resolve → validate → permit → execute → cap). Hooks have no purchase.

**Mark II shape:** keep the single function (no behavior change), but factor
the stage boundaries into two zero-cost extension hooks with fixed names:

```python
class ToolHook(Protocol):
    async def pre_tool(self, name: str, args: dict, ctx: ToolContext) -> HookVerdict | None:
        """After permit, before execute. May veto (becomes is_error) or annotate."""
    async def post_tool(self, name: str, args: dict, result: ToolResult, ctx: ToolContext) -> ToolResult:
        """After execute, before cap. May transform or replace the result."""
```

- `ToolContext` gains `hooks: list[ToolHook]` (empty list in Mark II — the
  loop pays one `for` over an empty list; no other cost).
- Placement is fixed and documented: `pre_tool` runs **after** permission
  resolution (a hook cannot see, let alone approve, what the gate denied),
  `post_tool` runs **before** result capping (hooks see full output; the
  spill discipline still bounds what reaches the model).

**Mark III payload:** Claude-Code-style operator hooks (shell commands or
plugin callables on tool events), audit sinks, result redactors. The
Warden/engine never changes — hooks live entirely inside dispatch.

**Mark II consumer:** none (empty list) — but W18's per-tool usage logging
and the journal's tool records are written against these stage names so the
vocabulary is already load-bearing.

---

## Seam 4 — Event flow (`emit` / `JobEvent`)

**Today:** already a seam in fact — `Warden.emit` is an injected callable,
`JobEvent.type` is an open string vocabulary, unknown types drop harmlessly
at both consumers (peer's `_CHAT_FORWARD` filter, Mark VI's `_EVENT_MAP`).

**Mark II shape:** formalize, don't rebuild. `run_job` composes the emit
chain as an explicit ordered fan-out — `[journal_sink (W17), usage_sink
(W18), transport_emit]` — behind one `EventFan` helper, replacing the ad-hoc
lambda wrapping. Sinks are `async (JobEvent) -> None`; a sink raising is
caught-and-logged, never job-fatal.

**Mark III payload:** plugin event subscribers (metrics exporters, external
notifiers, live dashboards) append a sink. Nothing else changes.

**Registered event vocabulary** (append-only registry table kept in
`protocol.py`): `started chunk tool tool_result done error usage (W18)
task_update (W10) permission_request/response (W13, frames not events)
compact (W2)`.

---

## Seam 5 — Model provision (`model/factory.py` + `providers.py`)

**Today:** `_OPENAI_COMPAT` dict + `parse_model_ref` — already
registry-shaped, but module-private and closed.

**Mark II shape:** expose registration:

```python
def register_provider(name: str, builder: Callable[[str, ForgeSettings], Model]) -> None
```

Builtin providers register through the same call at import of
`model/factory.py` (law 2 exception, documented: the *builtin* table is core,
not an extension — the function exists so a Mark III plugin can call it from
its own load path). `Model` protocol additions in Mark II (W1's
`UsageReport`, W3's cache behavior) are **optional-by-default**: a model that
never yields usage still works (ledger estimates), one that ignores caching
still works (just costs more). That tolerance is what keeps third-party
model plugins cheap.

**Mark III payload:** provider plugins (new inference backends) without
touching `providers.py`.

---

## Seam 6 — Cell backends (`cell/factory.py`)

**Today:** `build_cell` switches on a backend string — registry-shaped,
closed set (`docker`, `subprocess`, `auto`).

**Mark II shape:** same `register_backend(name, builder)` treatment as Seam
5. The W8 background-process surface goes into the `Cell` ABC as **abstract**
methods — a Mark III backend (remote VM cell, Firecracker, k8s pod) must
implement the full contract or fail at registration, not at runtime. `auto`
resolution order stays core policy.

**Mark III payload:** exotic isolation backends as plugins.

---

## Seam 7 — Prompt composition (`agents/` + `gate/runner.py`)

**Today:** `AgentConfig.system_prompt` is one opaque string; W6 (repo
context) and W12 (git discipline) both want to append to it, which without a
seam becomes string-concatenation soup in `run_job`.

**Mark II shape:** system prompts are assembled from an ordered fragment
list at job assembly:

```python
@dataclass(frozen=True)
class PromptFragment:
    source: str       # "profile" | "shared:git" | "repo:CLAUDE.md" | …
    text: str

def compose_system_prompt(fragments: list[PromptFragment]) -> str
```

- Order is fixed policy: profile identity → shared discipline fragments
  (W12) → repo context (W6). Delimited sections with source labels.
- The composed result is what W3's cache breakpoint covers — composition
  happens once per job, before the first model call, so fragments cost
  nothing per-iteration.

**Mark III payload:** skills. A Claude-Code-style skill is, at its core, a
prompt fragment plus optional tools (Seam 1) — both seams exist, so a skill
loader is pure assembly-time code.

---

## Seam map — which workstream lands where

| Workstream | Seam(s) |
|---|---|
| W1 ledger / W3 caching | 5 (optional Model capabilities) |
| W2 compaction | core engine (not a seam — but the summarizer call goes through the job's `Model`, so provider plugins inherit it) |
| W4 chat continuity | wire (law 3) |
| W5 output pipeline | 6 (Cell contract) |
| W6 repo context | 7 |
| W7 search / W9 web / W10 tasks / W11 explore | 1 |
| W8 background procs | 6 (contract) + 1 (tools) |
| W12 git | 7 (fragment) + 1 (tool) |
| W13/W14 ask + allowlist | 2 |
| W15 counterparts | wire (law 3) |
| W16 retry | 5 (wraps any `Model`) |
| W17 journal / W18 telemetry | 4 (sinks) |

## What Mark III then costs

With these seams held, the Mark III plugin architecture reduces to: a
manifest format, a loader that reads it and calls the seven registration
surfaces above, and a trust model for third-party code. **No engine, cell,
dispatch, permission, or wire changes.** If any of those turn out to need
changing to admit a plugin, the seam was violated during Mark II — which is
exactly what this document exists to prevent.
