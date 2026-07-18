# Mark II — Trust: The Ask Round-Trip (W13–W15)

The workstream that changes Forge's relationship with the operator. Today the
safety gate is a wall: a gated-but-legitimate operation returns
`Permission denied … the operator must allow-list it explicitly` — but there
is **no mechanism to do so mid-job**. The `AllowList` has an `add()` that
nothing calls. Claude Code's defining trust feature is the interactive
prompt: *dangerous-but-intended operations get a human decision and then
proceed.* Mark II builds that, across three repos.

**Ordering note:** this is the only multi-repo workstream. It ships
Forge-first (frames sent, auto-deny on timeout — behaviorally identical to
today), Mark VI second (relay), Heartbreaker third (card). Mark VI's agents
router provably ignores unknown frame types, so each stage is safe to deploy
alone.

---

## W13 — The `ask` decision (Forge side)

### Permissions engine (`warden/permissions.py`)

`Decision.behavior` gains a third value: `"ask"`. The resolution chain
becomes:

```
1. session/persistent allow-list hit          → allow   (unless gate-matched)
2. tool.check_permissions                     → its opinion (deny wins)
3. SAFETY GATE match                          → ask     (was: deny)
4. plan mode + mutating tool                  → deny    (plan stays a wall)
5. default                                    → allow
```

The gate stops being a dead end and becomes a checkpoint. **What it never
becomes:** silent. Every gate match still refuses to proceed without an
explicit human yes — `ask` with no answer degrades to deny (timeout, below).
Plan mode is *not* an ask — review mode means review mode.

### Dispatch gauntlet (`warden/dispatch.py`)

Step 3 grows a branch:

```python
if decision.behavior == "ask":
    answer = await ctx.oracle.ask(tool.name, args_dict, decision.reason)
    if answer.approved:
        if answer.remember:
            ctx.permissions.allowlist.add(f"{tool.name}:{key}")   # + persist (W14)
    else:
        return ToolResult(f"The operator declined {tool.name!r}: "
                          f"{answer.note or decision.reason}", is_error=True)
```

`ctx.oracle` is a new `PermissionOracle` on `ToolContext` — an interface with
exactly one method. Two implementations:

- **`AutoDenyOracle`** — returns denied immediately. Used for: standalone
  `serve` mode without an operator channel, the offline demo, tests, and as
  the wrapped fallback everywhere.
- **`PeerOracle`** — the real one: sends a frame over the peer socket, parks
  on an `asyncio.Future` keyed by `ask_id`, resolves on the answer frame,
  auto-denies after `FORGE_ASK_TIMEOUT_S` (default **120**) or on socket
  loss. The loop is otherwise untouched — the ask parks *inside* one tool
  dispatch, so interrupt boundaries, transcript shape, and parallel-batch
  semantics all hold. (A parked unsafe tool blocks its sequential batch by
  design — you don't run the *next* mutation while the operator ponders the
  current one. Read-only tools in the same batch already completed; that's
  fine.)

### Wire frames (`gate/protocol.py`, peer additions)

```jsonc
// Forge → Mark VI
{"type": "permission_request", "agent_id": "optimus", "ask_id": "<uuid>",
 "job_id": "…", "chat_id": "…?",          // chat_id present on chat jobs
 "tool": "run_command",
 "action_key": "git push --force origin main",
 "reason": "command matches a high-blast-radius pattern (git push --force)",
 "timeout_s": 120}

// Mark VI → Forge
{"type": "permission_response", "ask_id": "<uuid>",
 "approved": true, "remember": false, "note": ""}
```

`ForgePeer._dispatch` gains one `elif` for `permission_response` → resolve
the parked future. On `_serve_one` teardown, all parked futures resolve to
denied (socket loss ≠ hang).

### Tests (`test_ask_flow.py`)

Scripted end-to-end with a fake oracle: approve → tool executes; deny →
is_error result, loop continues; approve+remember → second identical call
skips the ask; timeout → deny; socket-drop → deny; plan mode still denies
without asking; **gate-matched action can never sneak through via
remember** (allow-list is checked *after* the gate in `resolve` — property
test over the chain ordering).

---

## W14 — Persistent allow-list

**Problem.** `AllowList` is constructed empty per job (`allowlist or
AllowList()` in `run_job`). "Remember" from W13 would evaporate at job end.

**Design.** JSON file at `FORGE_ALLOWLIST_PATH` (default
`./.forge/allowlist.json`), shape `{"entries": ["run_command:git push*"]}`:

- Loaded once at process start (peer `main()` / server startup), shared
  across jobs — the store object is process-wide, handed into `run_job`.
- `add()` from an approved-with-remember ask appends + writes atomically
  (tmp + rename).
- Operator-editable by hand; malformed file → loud warning, empty list
  (fail-safe direction: forget approvals, never invent them).
- **Scope guard unchanged:** the allow-list remains step 1 of the chain and
  is *bypassed* when the gate matches — remembering an approval means "stop
  asking me about this exact action", enforced as: a remembered gated action
  short-circuits the **ask**, not the gate. Implementation: `resolve` checks
  the allow-list a second time at step 3 — a gate match that is allow-listed
  returns ALLOW (the operator's standing decision) rather than ask. The
  first check (step 1) continues to serve non-gated entries.
- Wildcards: the existing `fnmatch` glob semantics; the Heartbreaker card
  offers "remember exact" only — glob entries are hand-written, deliberate
  acts.

**Tests:** persistence round-trip, atomicity (crash-sim between tmp and
rename), malformed-file recovery, the gated-but-remembered precedence.

---

## W15 — Counterpart work in Mark VI and Heartbreaker

### Mark VI (`speda-mark6/packages/api`) — the relay

Additive only; no orchestrator/loop changes:

1. **`app/routers/agents.py`** (the `/agents/ws/{agent_id}` receive loop):
   two new `msg_type` branches —
   - `permission_request` → store `{ask_id, agent_id, tool, action_key,
     reason, expires_at}` on a small `app.state.pending_asks` dict **and**
     push to Heartbreaker over the live user channel: reuse the notification
     path a `push`-mode automation uses today, plus a `pending-asks` poll
     endpoint (below) as the guaranteed fallback. If the ask arrived on a
     **chat job** (`chat_id` present), additionally inject a synthetic
     `chat_event {type:"permission_request", data:{…}}` into that stream so
     an open Heartbreaker chat renders it inline — `external_proxy`'s
     `_EVENT_MAP` gains one entry mapping it to a new SSE type the UI
     understands.
   - Expiry sweep piggybacks on the existing heartbeat handling.
2. **`GET /agents/asks` + `POST /agents/asks/{ask_id}`** (new, thin router —
   Rule 1: zero logic, calls a small service): list pending; answer one →
   sends `permission_response` down the peer socket via `WebSocketManager.
   send()` and clears the entry. `X-API-Key` auth like everything else.
3. **`dispatch_agent` skill** (from TOOLBELT W9): grow the optional
   `network: bool` arg → `task_dispatch` frame carries it →
   `job_from_task_dispatch` maps it into `JobConstraints.network`. One line
   in three places.

### Heartbreaker (`packages/heartbreaker` + the Android port) — the card

- **The approval card**: agent-tinted glass card showing tool, the exact
  `action_key` (monospace, full — never truncate the command being
  approved), the gate's reason, a live countdown to timeout, and three
  controls: **APPROVE**, **APPROVE + DON'T ASK AGAIN** (exact key only),
  **DENY** (optional note field).
- Surfaces: inline in the chat stream when it arrives as a `chat_event`
  (chat jobs — the common case, since the operator is watching); as a
  notification + a badge on the comms tray for dispatched background jobs.
- Android: same card in the comms tray surface ported earlier; the poll
  endpoint makes it work without new push infrastructure.

### Deploy sequence (safe at every step)

1. Forge ships W13 with `PeerOracle` — unanswered asks auto-deny at 120 s:
   behavior is identical to Mark I except for a 2-minute pause on gated ops.
2. Mark VI ships the relay + endpoints — asks now reach the owner's surfaces.
3. Heartbreaker ships the card — the loop closes.

### Acceptance (the TRUST slice of the master done-signal #3)

Live: a chat job runs `git push --force` → card appears inline in
Heartbreaker → APPROVE → push executes, job completes. Repeat with DENY →
model reports it could not push and finishes gracefully. Repeat with the card
ignored → 120 s → same graceful path. Repeat with APPROVE+REMEMBER → second
job force-pushes without a card.

---

## Config summary

| Var | Default | |
|---|---|---|
| `FORGE_ASK_TIMEOUT_S` | `120` | W13 |
| `FORGE_ALLOWLIST_PATH` | `./.forge/allowlist.json` | W14 |
