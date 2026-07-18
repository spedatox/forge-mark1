# Mark II — Resilience & Accounting (W16–W18)

The workstreams that make Forge survivable and measurable: transient-error
retry, crash-proof jobs, and knowing what a job cost.

---

## W16 — Transient retry (`forge/model/retry.py`, new)

**Problem.** `Warden.run` wraps the model stream in one `try/except` that
converts **any** exception into an ERROR terminal. One Anthropic 529, one
network blip at iteration 25 of 30 — job dead, work lost. The existing
fallback chain (`FORGE_LLM_FALLBACK_CHAIN`) only covers stream-*open* failure
and switches providers, which is the wrong first response to a transient.

**Design — classification first.** A small classifier over the exception:

| Class | Examples | Response |
|---|---|---|
| **Transient** | 429, 500, 502, 503, 529, `overloaded_error`, timeouts, connection resets | Retry same model |
| **Permanent** | 400 invalid request, 401/403 auth, context-length-exceeded | No retry — surface immediately (context-length additionally signals W2 compaction, below) |
| **Unknown** | anything else | One retry, then surface |

Detection: prefer typed SDK exceptions (`anthropic.APIStatusError.status_code`,
`openai.APIStatusError`), fall back to string sniffing for the OpenAI-compat
providers' looser errors. The table lives in one module both model classes
share.

**Design — the wrapper.** `stream_with_retry(model, *, attempts=4,
base_delay=2.0, signal)`: capped exponential backoff with ±25 % jitter
(2 s → 4 s → 8 s, honoring `retry-after` headers when present), interruptible
— the backoff sleep races `signal.wait()` so an operator abort during a retry
wait exits immediately.

**The partial-turn rule — the subtle part.** A stream can die **mid-turn**
with text already yielded and events already emitted to Heartbreaker. Retrying
the turn as-if-fresh would duplicate that text in the transcript and on
screen. Rule: a retry **discards the partial turn entirely** — the engine
buffers the turn's `assistant_content` and only appends to `state.messages`
after the stream ends cleanly (this is already its structure — the append
happens post-stream), and emits a
`chunk: "\n[connection lost mid-response — retrying…]\n"` marker so the
operator understands the visible text restart. No transcript surgery needed.

**Interaction with the fallback chain:** retries exhaust on the primary model
first, *then* the existing chain advances to the next provider (which gets its
own, shorter, 2-attempt budget). Chain order and semantics unchanged.

**Context-length special case:** a context-length-exceeded permanent error
does not surface as ERROR when W2 exists — it triggers an emergency
compaction and one re-attempt. (Belt over W2's threshold suspenders: the
threshold estimate can undershoot on non-Anthropic providers whose usage
reporting is estimated.)

**Seam.** `engine.py`'s `async for ev in self.model.stream(...)` becomes
`async for ev in stream_with_retry(self.model, ...)`. The engine's own
`except` stays as the last-resort net.

**Config.** `FORGE_RETRY_ATTEMPTS` (4), `FORGE_RETRY_BASE_DELAY_S` (2.0).

**Tests** (`test_retry.py`): scripted model raising 529×2-then-success →
COMPLETED with 3 stream calls; permanent 400 → immediate ERROR; abort during
backoff → ABORTED promptly; partial-turn discard (text yielded before the
raise never appears twice in `state.messages`); retry-exhaustion → fallback
chain advances.

---

## W17 — Job journal & resume (`forge/gate/journal.py`, new)

**Problem.** Jobs exist only in process memory. A peer crash, host reboot, or
deploy mid-job loses everything: Mark VI eventually times out (180 s /
600 s), the Cell workspace holds half-finished work nobody can continue, and
the transcript that explains what happened is gone. "In-flight runs abort
cleanly on disconnect" — cleanly, but totally.

**Design — journaling.** Append-only JSONL per job at
`FORGE_JOURNAL_DIR` (default `./.forge/journal/`) / `{job_id}.jsonl`:

```jsonc
{"t": "meta",     "request": {…JobRequest…}, "agent": "optimus", "started": 1721…}
{"t": "message",  "i": 3, "msg": {…one transcript message…}}     // appended as produced
{"t": "compact",  "i": 14, "kept_from": 9}                        // W2 replaces prefix
{"t": "tasklist", "state": [...]}                                 // W10 snapshots
{"t": "terminal", "reason": "completed", "final": "…"}
```

- The journal attaches as an **event sink** on Seam 4's `EventFan` for
  everything event-shaped (tool records, usage, compaction markers), plus
  direct `journal.append()` calls at the three `state.messages` mutation
  sites for transcript records (messages aren't events — they're state; the
  sink alone can't reconstruct them).
  Fsync per tool-cycle, not per delta (a crash loses at most the current
  turn, which resume regenerates anyway).
- A `terminal` record marks the journal complete; a sweeper deletes journals
  older than `FORGE_JOURNAL_KEEP_DAYS` (7).
- The journal is also the **audit trail** — answering "what exactly did
  Optimus run on the server last night" becomes `grep` instead of archaeology.

**Design — resume.** On peer startup, before connecting: scan for journals
with no `terminal` record. For each orphan:

1. Rebuild `LoopState.messages` from `message` records (respecting the last
   `compact` record's replacement semantics — replay is "load the latest
   consistent transcript", never "re-run the events").
2. **Never re-execute tools.** The Cell workspace (subprocess backend: on
   disk; Docker backend: container gone but the bind-mounted workspace
   persists) is in its post-crash state and the transcript already contains
   the results of everything that ran. Resume appends one user message:
   `[The execution peer restarted. The workspace reflects all work above.
   Re-verify anything critical (git status, test run) before continuing,
   then proceed with the task.]`
   — and hands the state to a fresh Warden. Read-before-write grounding plus
   that preamble makes double-execution structurally impossible and
   stale-assumption recovery the model's explicit first step.
3. Announce the resumed run to Mark VI: `task_dispatch` jobs send their
   `task_result` on completion exactly as if never interrupted (Mark VI's
   dispatcher tolerates late results — the comms-tray row resolves);
   `chat_request` jobs are **not** auto-resumed (their Heartbreaker stream is
   gone and the owner has likely moved on) — their journals park for
   `forge jobs resume <id>` manual resumption, and get a terminal record
   marking them `orphaned`.
4. Cap: resume at most `FORGE_RESUME_MAX` (2) jobs concurrently; older
   orphans park for manual resume.

**New CLI:** `python -m forge jobs list|show <id>|resume <id>|clean` — the
operator's window into the journal dir (also the recovery path for parked
orphans).

**Interaction with W13:** a parked ask that dies with the process resolves to
deny on resume (the ask context is gone; re-attempting the gated action will
re-ask through the live channel).

**Tests** (`test_journal.py`): append/replay round-trip equals in-memory
transcript; kill-9 simulation (truncated final line tolerated — JSONL partial
last line dropped); resume-preamble presence; no-tool-re-execution property
(scripted model, side-effect counter); chat-vs-dispatch resume policy;
sweeper.

---

## W18 — Usage telemetry (rides on W1's ledger)

**Problem.** `Terminal` reports `iterations` — the one number that correlates
with neither cost nor progress. The operator of an execution peer needs to
know what a job cost, live and after.

**Design.**

- **`Terminal` grows `usage`**: the final `TokenLedger` snapshot — input,
  output, cache-read, cache-write totals, plus `estimated_cost_usd` computed
  from a static per-model price table (`forge/model/pricing.py`; prices for
  the fleet's known models, `None` → cost omitted, table is data not logic).
- **Live:** the engine already receives `UsageReport` per turn (W1); a
  usage **sink** on Seam 4's `EventFan` emits a `usage` JobEvent each turn:
  `{"input": …, "output": …, "cache_read": …, "iteration": …,
  "fullness": 0.34, "est_cost_usd": 0.42}`.
- **Peer:** `task_result` frames gain a `usage` field (additive — Mark VI's
  handler passes dict payloads through; the comms tray can render
  `"$0.42 · 41k in / 6k out · 62% cached"` on the result row later).
- **Logs:** one structured `job_usage` log line at terminal — the grep-able
  record for "what did this month of Optimus cost".
- Non-Anthropic estimate-based numbers are flagged `"estimated": true` so
  the UI can show `~`.

**Tests:** price table math, terminal snapshot correctness across a scripted
multi-turn run, estimated-flag propagation.

---

## Config summary

| Var | Default | |
|---|---|---|
| `FORGE_RETRY_ATTEMPTS` | `4` | W16 |
| `FORGE_RETRY_BASE_DELAY_S` | `2.0` | W16 |
| `FORGE_JOURNAL_DIR` | `./.forge/journal` | W17 |
| `FORGE_JOURNAL_KEEP_DAYS` | `7` | W17 |
| `FORGE_RESUME_MAX` | `2` | W17 |
