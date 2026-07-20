# Forge HQ — implementation plan

Companion to [REFERENCE_ARCHITECTURE.md](REFERENCE_ARCHITECTURE.md), which
records *what the reference does*. This document is *what Forge builds, in what
order, and how each piece is proven*.

It supersedes the build order at the bottom of that document. Two things changed
after a closer read: the reference's thresholds turned out to be **absolute token
budgets rather than percentages** (§H2), and the navigation tools moved from last
to first once it became clear they are what *prevents* context pressure rather
than what survives it (§ordering).

MARK2's five documents remain the reference for W13–W15 (trust) and W17
(journal); where this plan overlaps them it says so and states the amendment.

---

## Ordering, and why it is not the obvious one

The instinct is to fix survival first — compaction, retry — and add capability
after. That is backwards for this codebase. Forge's context fills because its only
way to look at a repo is to read whole files; a job that greps instead of reading
spends a fraction of the window and needs compaction far later. Navigation is not
a comfort feature, it is the cheapest context mechanism available.

So: **make it stop wasting context (H1–H3), then make it survive filling it
anyway (H4–H6), then make it ask (H7), then make it extensible (H8–H10).**

| # | Work | Depends on | Size |
|---|---|---|---|
| **H1** | Navigation: `grep`, `glob`, ranged `read_file` | — | M |
| **H2** | Token ledger | — | S |
| **H3** | Tool-result budget | — | S |
| **H4** | Error classification + transient retry | done: state machine | M |
| **H5** | Compaction | H2 | M |
| **H6** | Recoverable errors: withhold + recover | H4, H5 | S |
| **H7** | Permission: `ask`, rule sources, `updated_args` | — | L |
| **H8** | Per-input tool flags | — | S |
| **H9** | The seams (MARK2_SEAMS W0) | H8 | M |
| **H10** | Skills, hooks, MCP over the seams | H9 | L |

Phases 0 (batch ordering) and the loop state machine are already committed on
`hq/loop-foundations`.

---

## H1 — Navigation: `grep`, `glob`, ranged `read_file`

**The gap.** Forge's only repo instruments are whole-file `read_file` and
shelling out. The reference's Grep description contains an explicit steering
line — *"ALWAYS use Grep for search tasks. NEVER invoke `grep` or `rg` as a Bash
command"* — because a harness-owned search tool gets correct permissions, bounded
output, and declared parallel-safety, and a shelled-out one gets none of those.

**Build** — `forge/tools/search.py` (new), `forge/tools/files.py` (changed).

`grep`: `pattern`, `path`, `glob`, `output_mode` ∈ `content` |
`files_with_matches` (default) | `count`, `context_lines`, `max_matches`,
`case_sensitive`, `multiline`. Try `rg --json` in the Cell, detected once;
fall back to a pure-Python walker over the same pruned tree
(`.git`, `node_modules`, `.venv`, `__pycache__`, `.forge_spill`), binary-sniffed.
Both paths render identically — that parity is the test.

The three output modes matter more than they look: `files_with_matches` as the
**default** is what keeps a broad search cheap, and it is the mode a model
reaches for when orienting. Content mode is the expensive one and should be
chosen deliberately.

`glob`: `pattern`, `path`. Sorted by mtime descending — recently-touched first,
which is almost always what the question meant. Capped at 200.

`read_file` gains `offset` / `limit`, and its output becomes line-numbered
(`   412→ content`). Line numbers are what make `edit_file`'s exact-match
anchoring reliable on large files, and they compose with grep's `path:line`
output. Two caps, following the reference's measured design: a **byte gate on the
whole file** checked before reading (a 100-byte error beats 25 K tokens of
truncated content — the reference tested truncation instead and reverted it), and
a token cap on the produced slice.

Freshness: a ranged read records the hash of the **full** file, so edit-grounding
still covers what was not shown.

One extra, cheap and worth it — the reference returns a stub rather than content
when a file is re-read unchanged (`FILE_UNCHANGED_STUB`): *"the content from the
earlier Read tool_result is still current."* Forge's `FileStateCache` already
holds the hash needed to do this. It costs nothing and removes the single most
common duplicate in a long transcript.

**Tests.** rg-present / rg-absent parity on a fixture tree; prune list; caps;
ranged numbering; freshness-after-ranged-read edit round-trip; unchanged-re-read
stub.

**Profiles.** `CODING_TOOLS` grows `grep`, `glob`.

---

## H2 — Token ledger

MARK2 W1 as written, with one correction from the source.

**The correction.** MARK2 specifies `fullness = input_tokens / context_limit` and
a `0.80` trigger. The reference uses **absolute budgets**:

```
effective_window = context_window − min(max_output_tokens, 20_000)
compact_at       = effective_window − 13_000
blocking_limit   = effective_window −  3_000
```

For a 200 K window: effective 180 K, compact at 167 K, block at 177 K. The
percentage is an emergent ~83 %, not a configured 80 %. This matters because the
reserve is the space the *summary call itself* needs — a percentage threshold on
a small-context local model reserves too little and the compaction call fails
exactly when it is needed.

**Build** — `forge/warden/ledger.py`. `TokenLedger` on `LoopState`, fed by an
optional `UsageReport` event from `Model.stream` (optional per Seam 5: a provider
that never reports usage falls back to a `chars/4` estimate). Thresholds computed,
not configured; `FORGE_CONTEXT_LIMIT` overrides the window for local models.

---

## H3 — Tool-result budget

**The gap.** Forge caps each result at `max_result_chars` and spills the
overflow. Twenty 19 KB results each pass that cap and together bury the window.

**The reference's shape**, and its numbers:

- per-tool cap `min(declared, 50_000)`;
- per-**message** aggregate ceiling of `200_000` chars across one turn's batch,
  each message evaluated independently;
- over budget → persist the **largest** blocks first until under, replacing each
  with a 2 000-byte preview plus the path, wrapped in `<persisted-output>`;
- `read_file` is exempt (`Infinity`), because persisting a Read produces a
  file the model reads back with Read — a circular loop.

**Build** — extend `dispatch._cap_result` into a two-stage budget: per-result as
today, then a batch pass in `engine._run_tools` before the results become a
message. Spill paths reuse `.forge_spill/`, which the model can already `grep`
(H1 makes that genuinely useful — the spill stops being a dead end).

Subsumes and widens MARK2 W5, which fixes only the Cell-truncates-before-spill
ordering bug.

---

## H4 — Error classification + transient retry

MARK2 W16, landing in the failure boundary the state machine created.

Three classes, one classifier module shared by both model adapters: **transient**
(429/500/502/503/529, `overloaded_error`, timeouts, resets) → retry same model,
capped exponential backoff with jitter, honouring `retry-after`, interruptible by
the abort signal; **recoverable** (context-length-exceeded) → H6; **permanent**
(400, 401/403) → surface.

The partial-turn rule is already satisfied: `_Turn` is discarded structurally, so
a retry cannot duplicate text. It emits one marker chunk so the operator
understands the visible restart.

`ContinueReason` grows `RETRY_TRANSIENT`. Retries exhaust on the primary before
the existing fallback chain advances.

---

## H5 — Compaction

MARK2 W2's design is sound and stands: head / body / tail, cut only at a
user-message boundary after a complete tool cycle, `FileStateCache` cleared,
hard-truncation fallback. Four amendments from the source:

1. **Layer it.** Before summarizing, drop old tool *results* by `tool_use_id`
   without inspecting content. Cache-safe by construction, needs no model call,
   and on a coding job most of the window is tool output — this alone will often
   clear the threshold and keep the transcript granular. Summarize only what it
   cannot reach.
2. **Circuit-breaker.** Stop after 3 consecutive failures. The reference logs
   1,279 sessions that retried 50+ times, one reaching 3,272, burning ~250 K API
   calls a day. An unbounded compaction retry is a money fire.
3. **The summary prompt is a 9-section template**, not a paragraph — request and
   intent, technical concepts, files and code sections, errors *and their fixes*,
   problem solving, **all user messages verbatim**, pending tasks, current work,
   next step with direct quotes. It is preceded by a hard no-tools preamble and
   followed by a no-tools trailer, because models attempt tool calls during
   summarization and a rejected call wastes the single turn. Forge's version
   drops sections 6 and 9's quoting only if there is a reason; there is not.
4. **Reserve output space** — the `20_000` from H2 is what this call spends.

---

## H6 — Recoverable errors: withhold and recover

The mechanism from §2 of the architecture doc, buildable only once H5 exists.

A context-length error is **not emitted**. It is held, H5 runs, the turn is
re-attempted, and the error surfaces only if compaction could not free space —
terminal `CONTEXT_EXHAUSTED`, a new `StopReason`. `ContinueReason` grows
`RECOVERED_CONTEXT`.

The failure mode to engineer against, stated plainly in the source: a withhold
whose recovery never runs silently eats the error. The withhold condition and the
recovery condition must be the same expression, evaluated once.

---

## H7 — Permission: `ask`, sources, `updated_args`

MARK2 W13/W14/W15 as written, widened by three things the source makes clear:

- **`validateInput` is a stage of its own**, before permissions. Forge collapses
  schema validation and permission into one step; a tool that wants to reject
  semantically-invalid-but-schema-valid input has nowhere to stand.
- **A decision returns `updated_args`.** That channel is how a permission layer
  rewrites a path or scopes a command without the tool knowing. Retrofitting it
  later means touching every call site.
- **Rules carry a source** (`operator`, `project`, `policy`, `session`). Needed to
  answer "why was this allowed", and so policy can outrank preference.

**Forge keeps its own default.** The reference's unmatched default is *ask*;
Forge's is *allow*, because an autonomous peer with no operator attached must
make progress. Forge's compensating control is the bypass-immune gate, which the
reference has a narrower analogue of (`safetyCheck` survives hooks and bypass
mode, but escalates to *ask*, where Forge denies). Keep Forge's, and keep
MARK2_SEAMS' law: plugins may tighten, never loosen.

---

## H8 — Per-input tool flags

`is_concurrency_safe`, `is_read_only`, `is_destructive` become methods taking
validated args. `RunCommand` can then declare `ls` safe and `rm` not, instead of
serializing every shell call. Fail closed if the check raises.

Small, and it must land before H9 — the seam protocols embed these signatures.

---

## H9 — The seams

MARK2_SEAMS W0, unchanged in design, with one amendment: **tool provision must be
re-callable mid-job.** The reference refreshes its tool list *between turns inside
one query*, so an MCP server that finishes connecting mid-job becomes usable
without restarting. An assembly-time-only `ToolProvider` fold cannot express that,
and MCP is the whole point of the seam.

---

## H10 — Skills, hooks, MCP

Over H9's registration points, in the reference's own shapes:

- **Skills**: a directory of markdown files with frontmatter (`name`,
  `description`, `allowed-tools`, `model`), loaded per source. A skill is a
  prompt fragment plus a tool allowlist — Seam 7 plus Seam 1, both of which exist
  by then.
- **Hooks**: `pre_tool` / `post_tool` first (Seam 3). A hook returns
  allow/deny/ask, may supply `updated_args` (H7's channel), may inject context,
  may prevent continuation. **The scar to inherit:** a hook that forces
  continuation must not reset recovery guards — the reference documents an
  infinite loop where a stop-hook reset the compaction guard.
- **MCP**: one `ToolProvider` per server, each remote tool wrapped in a `Tool`
  whose flags fail closed unless the server's annotations say otherwise.
- **Plugin manifest**: the reference's shape is a directory with
  `commands` / `agents` / `skills` / `hooks` / `mcpServers` paths. Forge's subset
  is skills + hooks + MCP servers.

---

## What this plan does not build

Carried forward from the architecture doc's rejected list, plus two additions now
that the source is better understood: **streaming tool execution** (revisit after
H4–H6 settle), **deferred tool loading / ToolSearch** (justified past ~40 tools),
**context collapse** (its value assumes an interactive user), the **six permission
modes**, **tool-use summaries**, and the **classifier-based auto mode** — which is
a second LLM in the permission path, and MARK2 §6 already rejected LLM risk
classification on the grounds that the operator is the risk assessor.
