# Mark II — Context & Continuity (W1–W6)

The endurance workstreams: the ones that decide whether a job survives hour
three. Everything here lives in the Warden/model layer; no tool changes.

---

## W1 — Token ledger (`forge/warden/ledger.py`, new)

**Problem.** The loop has no idea how full the window is. `max_iterations` is
doing double duty as a context guard, which it isn't.

**Design.** A `TokenLedger` owned by `LoopState`:

```python
@dataclass
class TokenLedger:
    context_limit: int              # from the model (200_000 default Anthropic)
    input_tokens: int = 0           # last turn's prompt size — THE fullness signal
    output_tokens: int = 0          # cumulative
    cache_read_tokens: int = 0      # cumulative (W3 effectiveness metric)
    cache_write_tokens: int = 0

    @property
    def fullness(self) -> float:    # 0.0–1.0 of context_limit
        return self.input_tokens / self.context_limit
```

**Where the numbers come from.** Anthropic's streamed final message carries
`usage` (input_tokens, output_tokens, cache_creation_input_tokens,
cache_read_input_tokens). The `Model` protocol grows one optional event:

```python
@dataclass
class UsageReport:                  # yielded once per turn, after content
    input_tokens: int
    output_tokens: int
    cache_read: int = 0
    cache_write: int = 0
```

`AnthropicModel.stream` yields it from `final.usage`. `OpenAICompatModel`
yields it from the chunk carrying `usage` when the provider sends one, else
estimates `len(json.dumps(messages)) // 4` — the ledger is a gauge, not a
bill, for non-Anthropic providers. The engine's event loop gains one `elif
isinstance(ev, UsageReport)` branch that updates `state.ledger` and forwards a
`usage` JobEvent (consumed by W18 telemetry; Mark VI's proxy drops unknown
event types today, which is fine).

**Config.** `FORGE_CONTEXT_LIMIT` (default 200000) — override for smaller
local models.

**Tests.** `test_ledger.py`: usage accumulation across scripted turns;
fullness math; estimate fallback when a provider yields no usage.

---

## W2 — Compaction (`forge/warden/compaction.py`, new)

**Problem.** `state.messages` grows monotonically; the loop dies with a
provider error when the window fills. This is the single biggest blocker to
"absolute execution peer".

**Design — threshold.** At the **top** of each iteration (before the budget
check), if `ledger.fullness >= 0.80` (`FORGE_COMPACT_AT`, default 0.80),
run a compaction pass. Also emit a `chunk` event
`[context 80% full — compacting…]` so the operator sees it happen in
Heartbreaker.

**Design — the cut.** The transcript is split into three zones:

```
[ head ]  the original task message (messages[0]) — never compacted
[ body ]  everything up to the cut point           — summarized away
[ tail ]  the most recent N complete tool cycles   — kept verbatim
```

- The cut point may only fall at a **user-role message that contains no
  tool_result blocks**, or immediately **after** a complete
  assistant(tool_use) → user(tool_result) pair. Never between a `tool_use`
  and its `tool_result` — that is the transcript-corruption failure mode, and
  it is enforced by construction, not by validation after the fact.
- Tail size: keep the last `FORGE_COMPACT_KEEP_CYCLES` (default 5) complete
  cycles, always including every file-touching cycle among them.

**Design — the summary.** One non-streamed model call (same model, no tools):

> Summarize this execution transcript for an agent that will continue the
> work. Preserve: the goal; every file created/modified with a one-line note
> of what changed; commands run and their outcomes; discovered constraints,
> errors, and their fixes; the current plan state and immediate next step.
> Omit: file contents already applied, dead-end exploration, tool chatter.

The body is replaced by a single user message:

```
[COMPACTED HISTORY — {n} earlier iterations summarized]
{summary}
[The transcript resumes below. Files on disk reflect ALL work above, including
summarized work. Re-read any file before editing it.]
```

The `FileStateCache` is **cleared** on compaction — read-before-write
grounding forces fresh reads, which is exactly correct after a summary: the
model's memory of file contents is now the summary's, not the cache's.

**Design — failure.** If the summary call itself fails (after W16 retries),
fall back to a hard truncation cut (drop body, keep head + tail + a stub
notice). Degraded but alive beats dead.

**Engine seam.** One call inserted in `Warden.run`:

```python
if state.ledger.fullness >= self.compact_at:
    state = await compact(state, self.model, keep_cycles=..., emit=self.emit)
```

**Tests.** `test_compaction.py`: cut-point legality property test over
synthetic transcripts (random tool-cycle shapes → after compaction, every
tool_use has its tool_result adjacent, roles alternate legally); head/tail
preservation; file-cache cleared; fallback path; threshold trigger with a
scripted model reporting inflated usage.

---

## W3 — Prompt caching (`forge/model/anthropic_model.py`)

**Problem.** Every iteration re-sends system + tools + the whole transcript
uncached. A 30-iteration job pays for the same prefix ~30 times. This is the
cheapest large win in the whole plan.

**Design.** Anthropic cache_control breakpoints, added at request build time:

1. On the **last tool schema** in the tools array (caches all tool defs).
2. On the **system prompt** block (stable per job — W6's repo-context is
   appended once at job start, so it caches too).
3. On the **last content block of the second-to-last message** — the standard
   rolling-prefix pattern; each turn re-reads the previous turn's cache and
   extends it.

`system` becomes a block array `[{type: "text", text: ..., cache_control:
{type: "ephemeral"}}]`. Breakpoints 1–2 are static; 3 is applied per-request
to a **copy** of the trailing message (never mutate `state.messages`).

Non-Anthropic providers: no-op (their adapters ignore it; some cache
implicitly). No loop branching — this lives entirely inside `AnthropicModel`.

**Also while in this file:** `max_tokens` default 4096 → **16384**
(`FORGE_MAX_TOKENS`); at 4096 a long code-writing turn hits the length stop
mid-file. Note: engine currently ignores length-stops silently — with 16 K it
becomes rare; the ledger (W1) records output size so W18 can flag it.

**Acceptance.** A 10-iteration live job shows `cache_read_tokens` ≥ 80 % of
input tokens from iteration 2 onward (visible via W18 telemetry).

---

## W4 — Chat continuity (`forge/gate/protocol.py`)

**Problem.** `job_from_chat_request` calls `_last_user_text(history)` and
**throws away everything else Mark VI sent**. Every Heartbreaker turn with
Optimus starts from zero: "now fix the bug we discussed" is meaningless to it.
This is a silent, severe parity break with in-process agents — and the fix is
one function.

**Design.** `JobRequest` grows `history: list[dict] | None = None`.
`job_from_chat_request` passes the **full** history through (it is already
Anthropic content-block format — Mark VI speaks nothing else, per its Rule 8).
In `run_job`, seed the loop:

```python
state = LoopState(messages=[*(request.history or []), {"role": "user", "content": request.task}])
```

…except the last user message IS the task when history is present — so:
`messages = request.history` when the trailing message is the user turn, with
`request.task` retained only for journal/labeling. Sanitation at the seam:
strip Mark VI-side-only block types if any appear; tolerate empty history
(dispatch path unchanged — `task_dispatch` jobs stay single-task).

**Interaction with W2.** A long chat history may arrive already large;
compaction's threshold check at the top of iteration 1 handles it naturally —
this is why W2's check runs *before* the first model call, not after.

**Tests.** `test_protocol.py` additions: history passthrough; trailing-turn
handling; tool_use/tool_result pairs surviving the seam intact.

---

## W5 — Output pipeline fix (`forge/cell/base.py`, `tools/shell.py`)

**Problem.** Two caps fight each other. `Cell._cap` hard-truncates
stdout+stderr at `max_output_bytes` (100 KB) **inside the Cell**, so by the
time `dispatch._cap_result` runs its spill-to-file logic (which exists and
works), the full output is already gone. A 2 MB failing build log arrives as
100 KB + `…[truncated]` — and the actual error line was at the end, which
truncation cut. The model then debugs blind.

**Design.**

- `CommandResult` grows `stdout_path: str | None` / `stderr_path: str | None`.
- The Cell, when output exceeds the cap, writes the **full** stream to
  `.forge_spill/cmd_{hash}.{out,err}.txt` in the workspace and returns
  **head 20 KB + tail 20 KB** (the tail is where compiler errors live) plus
  the spill path.
- `RunCommand` renders: `exit_code`, head, `…[{n} bytes omitted — full output
  at {path}; grep it there]`, tail. Dispatch's generic spill remains as the
  second line of defense for other tools.
- `max_output_bytes` stays as the cap on what returns **in-band**; the spill
  file is exempt (it lives on disk, not in context).

**Tests.** `test_cells.py` additions: oversize output → spill file contains
the full bytes; in-band result carries head+tail+path; both backends.

---

## W6 — Repo-context discovery (`forge/gate/runner.py`)

**Problem.** The Warden works inside repos that carry their own conventions —
speda-mark6's `CLAUDE.md` is rule-dense and currently invisible to Optimus,
who will happily violate Rule 1 (logic in routers) inside the very repo that
forbids it.

**Design.** In `run_job`, after `repo_path` resolves:

1. Look for, in order: `CLAUDE.md`, `AGENTS.md`, `.forge/INSTRUCTIONS.md` at
   the repo root. First hit wins (`FORGE_REPO_CONTEXT_FILES` to override the
   list).
2. Cap at 20 000 chars (hard-truncate with a notice — a conventions file this
   large is the repo's problem).
3. Append to the system prompt under a delimiter:
   `\n\n═══ REPO CONVENTIONS ({filename}) — these govern all work in this
   repository ═══\n{content}`.

Appending to **system** (not a user message) means W3's cache breakpoint
covers it — read once, cached for the whole job. No per-directory descent
(Claude Code's nested-CLAUDE.md feature) in Mark II: repos in this fleet keep
one root file; revisit only if a real repo needs it.

**Tests.** discovery order, cap, absence (no-op), and that the Cell can still
read the file normally (no consumption side effects).

---

## Config summary (all on `ForgeSettings`, all `FORGE_*`)

| Var | Default | Workstream |
|---|---|---|
| `FORGE_CONTEXT_LIMIT` | `200000` | W1 |
| `FORGE_COMPACT_AT` | `0.80` | W2 |
| `FORGE_COMPACT_KEEP_CYCLES` | `5` | W2 |
| `FORGE_MAX_TOKENS` | `16384` | W3 |
| `FORGE_REPO_CONTEXT_FILES` | `CLAUDE.md,AGENTS.md,.forge/INSTRUCTIONS.md` | W6 |
