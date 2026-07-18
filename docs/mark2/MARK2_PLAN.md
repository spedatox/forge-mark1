# Forge Mark II — Master Plan

**Thesis:** Mark I proved the skeleton — the errors-as-results loop, the Cell
contract, the bypass-immune gate, the Mark VI wire link. Mark II is the upgrade
package that turns it from "runs short, supervised jobs" into an **absolute
execution peer**: one that survives multi-hour engagements, finds its way
around large codebases, runs servers while testing them, asks the operator
instead of dead-ending, and never loses a job to a transient 529 or a dropped
socket.

**This is an in-place evolution, not a rewrite.** Every workstream lands at an
existing seam; no file is restructured. The repo stays `forge-mark1`; "Mark II"
is the milestone. The README badge already promises it — this plan makes it
true.

**And it must leave Mark III the same courtesy.** Mark II may not hard-wire
any capability into the engine: everything lands behind the named extension
seams in [MARK2_SEAMS.md](MARK2_SEAMS.md), so that a Mark III plugin
architecture (MCP servers, hooks, operator skills) is a loader over existing
registration points — not a rewrite. That document is the enforcement
contract; every workstream below cites the seam it lands on.

---

## The gap, in one table

Measured against Claude Code as the reference execution harness:

| # | Gap | Severity | Workstream |
|---|---|---|---|
| 1 | No context compaction — long jobs die when the window fills | **Blocker** | [CONTEXT](MARK2_CONTEXT.md) |
| 2 | `chat_request` history is **discarded** (`_last_user_text`) — every Heartbreaker turn starts amnesiac | **Blocker** | [CONTEXT](MARK2_CONTEXT.md) |
| 3 | No prompt caching — every iteration re-bills the full transcript | **Blocker** (cost) | [CONTEXT](MARK2_CONTEXT.md) |
| 4 | Cell truncates output at 100 KB **before** dispatch's spill can save it — build logs lose the error | High | [CONTEXT](MARK2_CONTEXT.md) |
| 5 | No Grep/Glob/ranged-read — navigation is shelled-out and whole-file-only; >200 K-char files are unworkable | **Blocker** | [TOOLBELT](MARK2_TOOLBELT.md) |
| 6 | No background execution — can't run a dev server and test against it; 600 s hard ceiling | **Blocker** | [TOOLBELT](MARK2_TOOLBELT.md) |
| 7 | No web tools — `allow_network` exists but only shell `curl` benefits | High | [TOOLBELT](MARK2_TOOLBELT.md) |
| 8 | No task tracking — 20+-iteration jobs drop steps | Medium | [TOOLBELT](MARK2_TOOLBELT.md) |
| 9 | No sub-loop delegation — exploration pollutes the main context | Medium | [TOOLBELT](MARK2_TOOLBELT.md) |
| 10 | No git/gh workflow discipline | Medium | [TOOLBELT](MARK2_TOOLBELT.md) |
| 11 | Gate denies with no ask-the-operator path — legitimate gated ops dead-end the job | **Blocker** | [TRUST](MARK2_TRUST.md) |
| 12 | Allow-list is per-session, in-memory — approvals never persist | Medium | [TRUST](MARK2_TRUST.md) |
| 13 | Any stream exception → ERROR terminal — one 529 kills a 25-iteration job | **Blocker** | [RESILIENCE](MARK2_RESILIENCE.md) |
| 14 | Jobs are fire-and-forget — a peer restart loses everything | High | [RESILIENCE](MARK2_RESILIENCE.md) |
| 15 | No token/cost telemetry — `Terminal` counts iterations, not dollars | Medium | [RESILIENCE](MARK2_RESILIENCE.md) |
| 16 | Repo's own `CLAUDE.md`/`AGENTS.md` conventions are invisible to the Warden | High (cheap) | [CONTEXT](MARK2_CONTEXT.md) |

**Explicitly out of scope for Mark II — but architected for:** MCP client,
hook system, and user-defined skills are **Mark III features whose seams Mark
II is required to ship** — tool provision, permission middleware, dispatch
hooks, event sinks, provider/backend registries, prompt fragments
([MARK2_SEAMS.md](MARK2_SEAMS.md)). Mark III then costs a manifest format and
a loader, zero engine changes. Genuinely out of scope with no seam
obligation: multimodal input, per-step workspace checkpoints/rewind, notebook
editing, multi-user anything. The "curated toolset, parameterized engine"
philosophy (§2) stands for what *ships*; the seams govern what can be
*added*.

## What already matches Claude Code — do not rebuild

For the record, so no workstream accidentally re-solves these: errors-as-results
discipline (`dispatch.py` gauntlet), read-before-write freshness via content
hash (`filestate.py` + `files.py`), parallel-safe tool batching gated on
declared safety (`engine._run_tools`), typed terminals (`state.py`), interrupt
at both boundaries, bypass-immune safety gate ordering
(`permissions.resolve`), oversize **tool-result** spill to `.forge_spill/`
(`dispatch._cap_result` — the gap is upstream in the Cell, see CONTEXT §4),
model-fallback chain on stream-open failure, secret-scrubbed Cell env, and the
1:1 Mark VI event vocabulary.

---

## Doc set

| Doc | Workstreams | New/changed files |
|---|---|---|
| [MARK2_SEAMS.md](MARK2_SEAMS.md) | The Mark III readiness contract — seven named extension seams every workstream must land on | `warden/toolsource.py`, `warden/dispatch.py` (hook points), `gate/runner.py` (assembly), `model/factory.py`, `cell/factory.py`, `agents/` (prompt fragments) |
| [MARK2_CONTEXT.md](MARK2_CONTEXT.md) | W1 token ledger · W2 compaction · W3 prompt caching · W4 chat continuity · W5 output pipeline · W6 repo-context discovery | `warden/ledger.py`, `warden/compaction.py`, `engine.py`, `anthropic_model.py`, `gate/protocol.py`, `cell/base.py`, `gate/runner.py` |
| [MARK2_TOOLBELT.md](MARK2_TOOLBELT.md) | W7 grep/glob/ranged read · W8 background processes · W9 web tools · W10 task list · W11 explore sub-loop · W12 git discipline | `tools/search.py`, `tools/process.py`, `tools/web.py`, `tools/tasks.py`, `tools/explore.py`, `cell/base.py` + both cells |
| [MARK2_TRUST.md](MARK2_TRUST.md) | W13 ask-decision + wire round-trip · W14 persistent allow-list · W15 Mark VI + Heartbreaker counterparts | `warden/permissions.py`, `warden/dispatch.py`, `gate/peer.py`, `gate/protocol.py`; Mark VI: `app/routers/agents.py`, `app/core/dispatch.py`; Heartbreaker: approval card |
| [MARK2_RESILIENCE.md](MARK2_RESILIENCE.md) | W16 transient retry · W17 job journal + resume · W18 usage telemetry | `model/retry.py`, `gate/journal.py`, `gate/runner.py`, `warden/state.py` |

Each doc carries its own design, exact seams, config additions, wire frames,
test plan, and acceptance criteria. Config additions across all docs are
consolidated in each doc's §Config; all are flat `FORGE_*` env vars on
`ForgeSettings` per the one-environment rule.

---

## Sequencing

Dependency-ordered. Within a phase, workstreams are independent and can
interleave.

```
Phase A  (endurance core — unblocks everything else)
├─ W0  seam scaffolding           ~small   ToolProvider + EventFan + prompt
│                                          composition + registry surfaces
│                                          (MARK2_SEAMS) — behavior-neutral,
│                                          lands first so every later
│                                          workstream registers through it
├─ W16 transient retry            ~small   engine.py call-site wrap
├─ W1  token ledger               ~small   prerequisite of W2, W18
├─ W3  prompt caching             ~small   pure win, no dependencies
├─ W4  chat continuity            ~small   protocol.py, high visible impact
└─ W5  output pipeline fix        ~small   cell/base + dispatch alignment

Phase B  (the long-job package)
├─ W2  compaction                 ~medium  needs W1
├─ W6  repo-context discovery     ~small
├─ W7  search/nav tools           ~medium
└─ W8  background processes       ~medium  Cell ABC + both backends

Phase C  (trust + reach)
├─ W13/W15 permission round-trip  ~large   three repos touched
├─ W14 persistent allow-list      ~small   rides on W13
├─ W9  web tools                  ~small
├─ W10 task list                  ~small
└─ W12 git discipline             ~small   mostly prompt + one tool

Phase D  (professional grade)
├─ W17 job journal + resume       ~medium
├─ W11 explore sub-loop           ~medium  benefits from W2 landing first
└─ W18 usage telemetry            ~small   needs W1
```

Rationale for Phase A first: retry (W16) protects every long test run of every
later phase; the ledger (W1) must exist before compaction can trigger; caching
(W3) makes all subsequent development iteration dramatically cheaper; chat
continuity (W4) is a one-function fix with owner-visible payoff today.

---

## The Mark II done signal

All six, verified on the Contabo/live link (mirrors Mark I's "all five, not
four" discipline):

1. **The endurance run.** A single dispatched job that requires ≥60 iterations
   and ≥3 compactions (e.g. "port this 40-file module and make its tests
   pass") reaches COMPLETED — no window death, no operator rescue.
2. **The server run.** A job scaffolds a FastAPI app, starts it in the
   background, curls it, fixes a bug it finds, restarts, re-verifies, and
   reports — inside one job.
3. **The ask.** A job hits the safety gate (e.g. needs `git push --force`),
   the approval card appears in Heartbreaker, the operator approves, the job
   continues and completes. Denial and 120 s timeout paths also verified.
4. **The pull of the plug.** Kill the peer process mid-job; restart it; the
   job resumes from its journal and completes; Mark VI receives exactly one
   terminal frame.
5. **The bill.** Terminal telemetry reports input/output/cache tokens and
   estimated cost per job; a cached iteration measurably cheaper than turn 1.
6. **The regression floor.** Entire Mark I test suite still green, plus the
   new suites (`test_ledger`, `test_compaction`, `test_search_tools`,
   `test_process_tools`, `test_ask_flow`, `test_retry`, `test_journal`);
   `python -m forge demo` still passes offline.
7. **The seam proof.** A throwaway out-of-tree module (`examples/plugin_probe/`)
   registers one tool via a second `ToolProvider`, one event sink, one prompt
   fragment, and one no-op `ToolHook` — using only the public registration
   surfaces, importing no core internals — and a demo job exercises all four.
   If the probe needs anything beyond MARK2_SEAMS' surfaces, Mark II is not
   done. (The probe is the Mark III plugin loader's proof-of-existence; it
   stays in-tree as the living conformance test.)

When all six hold: bump `pyproject.toml` version to `2.0.0`, update the README
status badge (which already says Mark II — it stops being aspirational), and
tag `mark-ii`.

---

## Risk register

| Risk | Mitigation |
|---|---|
| Compaction corrupts tool_use/tool_result pairing → API 400s | Compaction cuts only at **user-message boundaries after completed tool cycles**; property test asserts transcript well-formedness after every synthetic compaction (CONTEXT §2) |
| Non-Anthropic providers lack cache_control / count_tokens | Ledger falls back to chars/4 estimate; caching is Anthropic-only via a `Model` capability flag — the loop never branches on provider |
| Background processes leak on job end | Process table is owned by the Cell; `close()` kills the table; DockerCell containers die with the container (TOOLBELT §2) |
| Ask-round-trip deadlocks a job when Heartbreaker is closed | Hard 120 s timeout → auto-deny with a clear tool_result; the model adapts or finishes without it (TRUST §3) |
| Journal replay double-executes side-effectful tools | Resume replays the **transcript**, never re-executes tools; the Cell workspace is already in its post-crash state, and the model is told so via a resume preamble (RESILIENCE §2) |
| A workstream hard-wires past a seam under schedule pressure → Mark III forces the rewrite this plan exists to avoid | The seam probe (done-signal #7) fails CI the moment a capability is reachable only through core internals; MARK2_SEAMS amendment is the mandatory review path for any new seam |
| Multi-repo blast radius (three repos in W13/W15) | Wire frames are versioned and additive; Mark VI ignores unknown frame types today (verified in `agents.py` router), so Forge can ship first, Mark VI second, Heartbreaker third |
