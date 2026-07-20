# What the reference harness actually does — and where Forge diverges

Source read: `coding agent/` — `src/Tool.ts`, `src/query.ts`, `src/services/tools/
toolOrchestration.ts`, `src/services/compact/`, `src/utils/permissions/`,
`src/types/hooks.ts`. ~178 k lines of TypeScript; this document is the load-bearing
subset, not an inventory.

**The thesis of this document:** Forge's skeleton is not wrong. It is *thin* in
seven specific, nameable places, and in one place it is actually incorrect. Every
finding below is stated as: what the reference does → what Forge does → what to
build. Nothing here is a port. Several reference mechanisms are deliberately
rejected at the bottom.

---

## 1. The loop is a state machine with named transitions — not a `try/except`

The reference loop carries an explicit mutable `State` record between iterations
and every recovery path is a **continue site** that rewrites that state:

```ts
type State = {
  messages, toolUseContext, autoCompactTracking,
  maxOutputTokensRecoveryCount, hasAttemptedReactiveCompact,
  maxOutputTokensOverride, pendingToolUseSummary,
  stopHookActive, turnCount,
  transition: Continue | undefined,   // WHY the previous iteration continued
}
```

There are seven continue sites, each stamping a named reason:
`collapse_drain_retry`, `reactive_compact_retry`, `max_output_tokens_escalate`,
`max_output_tokens_recovery`, `stop_hook_blocking`, `token_budget_continuation`,
`next_turn`. The terminal is equally enumerated: `completed`, `blocking_limit`,
`model_error`, `prompt_too_long`, `image_error`, `aborted_streaming`,
`aborted_tools`, `hook_stopped`, `stop_hook_prevented`, `max_turns`.

The `transition` field exists for one stated reason — *"lets tests assert
recovery paths fired without inspecting message contents."* The recovery
machinery is designed to be testable by name.

**Forge:** `LoopState` is `{messages, iteration, last_text}`. One `try/except`
around the stream converts every failure into `StopReason.ERROR`. Four terminal
reasons total. There is no vocabulary for "the loop continued because it
recovered from X".

**Build:** widen `LoopState` with the recovery counters, add a `transition` field,
and expand `StopReason`. This is not cosmetic — it is the container every
subsequent reliability mechanism needs to live in. **Nothing else on this list can
be built cleanly until this exists.**

---

## 2. Recoverable errors are *withheld*, not surfaced

The subtlest mechanism in `query.ts`, and the one with the largest reliability
payoff. Inside the stream loop:

```ts
let withheld = false
if (contextCollapse?.isWithheldPromptTooLong(message, …)) withheld = true
if (reactiveCompact?.isWithheldPromptTooLong(message))     withheld = true
if (reactiveCompact?.isWithheldMediaSizeError(message))    withheld = true
if (isWithheldMaxOutputTokens(message))                    withheld = true
if (!withheld) yield yieldMessage
```

The error message is still pushed onto `assistantMessages` so the recovery
checks below can find it — but it is **not emitted to the consumer**. Recovery is
then attempted in cost order (collapse drain → reactive compact → surface). Only
if every stage fails is the withheld message finally yielded.

The comment explains exactly why: yielding early *"leaks an intermediate error to
SDK callers that terminate the session on any `error` field — the recovery loop
keeps running but nobody is listening."*

The withhold gate and the recovery gate are hoisted to a **single variable**
(`mediaRecoveryEnabled`) with the note that the two *must* agree, because a
withhold-without-recover silently eats the message. That is the failure mode this
pattern has to be engineered against.

**Forge:** `except Exception` → `emit({"type": "error"})` → `return ERROR`. The
error reaches Heartbreaker and the job is dead before any recovery could run.
MARK2's W16 (retry) sits at the *stream* level and would not help here, because
a context-length error is not transient — it is recoverable, which is a different
category.

**Build:** an error classifier with three outcomes — transient (retry),
**recoverable** (attempt a named recovery, withhold the error meanwhile),
permanent (surface). The recoverable class is the one Forge is missing entirely.

---

## 3. Compaction is five layers, cheapest first — not one pass

Per iteration, before the model call, in this exact order:

| Layer | What it removes | Cost |
|---|---|---|
| `applyToolResultBudget` | aggregate tool-result bytes across messages, replaced by pointers | free |
| `snipCompact` | stale spans | free |
| `microcompact` | old tool results **by `tool_use_id`, never inspecting content** | free, cache-safe |
| `contextCollapse` | a read-time projection over history; summaries live in a side store | one model call, granular |
| `autoCompact` | full 9-section summary of everything before the cut | one model call, lossy |

The ordering rationale is stated in the source: collapse runs before autocompact
*"so that if collapse gets us under the autocompact threshold, autocompact is a
no-op and we keep granular context instead of a single summary."* Losing
granularity is treated as a real cost, paid only when the cheaper layers cannot
close the gap.

Microcompact is cache-safe precisely because it keys on `tool_use_id` and never
looks at content — which is what lets it compose with the content-replacement
layer above it. That decoupling is the design, not an accident.

And compaction fires **two ways**: proactively on a token threshold, and
reactively on a real 413 from the API (`tryReactiveCompact`). The proactive
threshold is an estimate and estimates undershoot; the 413 path is the backstop.

**Forge:** nothing. MARK2 W2 designs exactly one layer — the full summary — with a
0.80 threshold and no reactive path.

**Build:** W2 as designed is correct as the *last* layer. Add, in order of value:
(a) tool-result budget with pointer replacement, (b) microcompact by tool_use_id,
(c) the reactive 413 path. (a) and (b) are cheap, need no model call, and will do
most of the work on real coding jobs where 90 % of context is tool output.

---

## 4. `isConcurrencySafe` is per-**input**, not per-tool

```ts
isConcurrencySafe(input: z.infer<Input>): boolean
```

Bash is concurrency-safe for `ls` and not for `rm`. Read is always safe. The
decision is made after the input is parsed, and if the check *throws*, the
partitioner catches and returns `false` — fail-closed at the call site, not just
in the default.

**Forge:** `is_concurrency_safe: bool` is a **class attribute**. `RunCommand` must
declare itself unsafe for every command it will ever run, so every shell call
serializes — including the read-only ones the model batches deliberately.

**Build:** make it a method on `Tool` taking validated args. Same for
`is_read_only` and `is_destructive` — the reference makes all three input-dependent
for the same reason.

---

## 5. Batching preserves order. Forge's does not. **This is a bug.**

The reference partitions into **runs of consecutive** safe calls:

```ts
if (isConcurrencySafe && acc[acc.length - 1]?.isConcurrencySafe) {
  acc[acc.length - 1].blocks.push(toolUse)     // extend the current run
} else {
  acc.push({ isConcurrencySafe, blocks: [toolUse] })   // start a new one
}
```

Batches then execute **in sequence**; within a safe batch, concurrently, capped at
10. A write between two reads splits the run into three batches, and the second
read observes the write.

Forge's `_run_tools` does this instead:

```python
safe   = [tu for tu in tool_uses if is_safe(tu)]
unsafe = [tu for tu in tool_uses if not is_safe(tu)]
if safe:   await asyncio.gather(...)      # ALL safe tools first
for tu in unsafe: ...                     # then all unsafe
```

It **reorders across the batch**. If the model emits `[write_file A, read_file A]`
in one turn — a completely ordinary verify-my-edit pattern — Forge runs the read
*first*, concurrently, and hands the model the pre-write contents. The result
blocks are re-sorted back into request order afterward, so the transcript looks
correct and the model has no way to detect it.

This is silent, it produces wrong answers rather than errors, and it is ~15 lines
to fix. It should be fixed independently of everything else on this list.

There is also no concurrency cap: 40 parallel greps become 40 simultaneous
subprocesses.

---

## 6. A permission decision may rewrite the tool input

```ts
checkPermissions(input, context): Promise<PermissionResult>
// → { behavior: 'allow', updatedInput }
```

Three-valued (`allow` / `deny` / `ask`), **async**, and it returns the input the
tool should actually run with. That `updatedInput` channel is how path rewriting,
sandbox overlays, and hook-supplied corrections reach the tool without the tool
knowing. Rules carry a **source** (`userSettings`, `projectSettings`,
`localSettings`, `policySettings`, `cliArg`, `command`) so the UI can answer
"*why* was this allowed" and so policy can outrank user preference. Six modes:
`default`, `plan`, `acceptEdits`, `bypassPermissions`, `dontAsk`, `auto`.

**Forge:** two-valued, synchronous, no input rewriting, one flat `AllowList` with
no source attribution, two modes. MARK2 W13/W14 adds `ask` and persistence —
correct, and it should also add the `source` field and `updated_args`, because
retrofitting either later means touching every call site.

The one thing Forge already has that the reference does **not**: a bypass-immune
gate that no rule, mode, or plugin can override. Keep it. It is the better design
for an autonomous peer with no human in the loop by default, and MARK2_SEAMS is
right to make "plugins may tighten, never loosen" a standing law.

---

## 7. Tool results have a *budget*, not just a cap

`maxResultSizeChars` per tool, plus `applyToolResultBudget` enforcing an
**aggregate** ceiling across all messages, replacing overflow with pointers and
recording each replacement so `/resume` can reconstruct what was dropped.

Read declares `Infinity` — with a stated reason: persisting a Read result
*"creates a circular Read→file→Read loop and the tool already self-bounds via its
own limits."*

**Forge:** per-result cap with spill (`_cap_result`), which is the right primitive
but only bounds one result. Twenty 19 KB results pass the cap individually and
still bury the window. MARK2 W5 fixes the Cell-truncates-before-spill ordering bug
— necessary, not sufficient.

**Build:** the aggregate budget, and mark `read_file` exempt.

---

## 8. Hooks are an event bus with the power to block

Nine events (`PreToolUse`, `PostToolUse`, `PostToolUseFailure`,
`UserPromptSubmit`, `SessionStart`, `SessionEnd`, `Stop`, `Notification`,
`PreCompact`). Hooks can veto a tool call, inject messages, and force loop
continuation — `stop_hook_blocking` is a continue site, and the source carries a
scar about it: resetting the reactive-compact guard there *"caused an infinite
loop: compact → still too long → error → stop hook blocking → compact → …
burning thousands of API calls."*

MARK2_SEAMS Seam 3 designs `pre_tool` / `post_tool` and places them correctly
(after permission, before capping). That is the right two to start with. The
lesson to carry forward is the scar, not the event count: **any hook that can
force continuation must not reset recovery guards.**

---

## 9. Everything is a registry, and tools refresh mid-turn

Tools, skills, agents, commands, MCP servers, output styles, hooks — each has a
loader and a registration point. `refreshTools()` is called *between turns inside
one query* so an MCP server that finishes connecting mid-job becomes available
without restarting the job.

MARK2_SEAMS already specifies six of the seven surfaces. It is a good document and
it should survive this reassessment intact. The single amendment: tool provision
must be **re-callable mid-job**, not assembly-time-only, or MCP servers can never
join a running job.

---

## 10. Deliberately rejected

Not everything in 178 k lines is worth having, and a plan that ports
indiscriminately is worse than one that ports nothing.

- **Streaming tool execution** (`StreamingToolExecutor` — start a tool before the
  assistant message finishes streaming). Real latency win, significant complexity:
  discard-and-recreate on fallback, synthetic results for in-flight tools on
  abort. Revisit only after the loop is a state machine.
- **Tool-use summaries** (a Haiku call per tool batch, for a mobile UI). Forge has
  no such surface.
- **`toolSearch` / deferred tool loading.** Justified above ~40 tools. Forge's
  curated set will not reach that, and the original decision to skip it (§2, open
  question 5) still holds.
- **The six permission modes.** `acceptEdits` and `dontAsk` are interactive-session
  ergonomics. An execution peer wants `act` / `plan` / `ask`.
- **Context collapse.** The most sophisticated layer, and the one whose value
  depends on an interactive user scrolling history. Autocompact plus microcompact
  covers the peer case.

---

## Build order

Dependency-forced, and it does not match MARK2's phasing — the loop state machine
now precedes everything, because it is the container the recovery paths live in.

```
0.  Batch-ordering fix                    ~15 lines, independent, silently wrong today
1.  Loop state machine                    LoopState + transitions + StopReason
    └─ nothing else lands cleanly first
2.  Error classification                  transient | recoverable | permanent
    ├─ transient  → retry            (MARK2 W16)
    └─ recoverable → withhold + recover   ← the missing category
3.  Per-input tool flags                  concurrency/read-only/destructive as methods
4.  Tool-result budget                    aggregate cap + pointers; read_file exempt
5.  Compaction, cheap layers first        microcompact → autocompact → reactive 413
6.  Permission: ask + source + updated_args   (MARK2 W13/W14, widened)
7.  Seams as specified                    MARK2_SEAMS, + mid-job tool refresh
8.  Toolbelt                              grep/glob/ranged-read, background procs, web
```

Items 0–2 are what turn Forge from "runs short jobs" into "does not lose a job".
Items 3–5 are what let it work in a large repo for hours. 6–8 are MARK2 as
written, and MARK2 remains the reference for those.
