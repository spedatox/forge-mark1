# Mark II — The Toolbelt (W7–W12)

Reach workstreams: navigation, long-running processes, the web, plan state,
delegation, and git discipline. Every tool below follows the Mark I contract —
`Tool` subclass, Pydantic `Args`, fail-closed harness flags, ≥3-sentence
model-facing description (final copy written at implementation; the
descriptions here are design intent). Registration is one line each in
`forge/tools/__init__.py::ALL_TOOLS` + the relevant profile allowlists.

---

## W7 — Search & navigation (`forge/tools/search.py`, new; `files.py` changed)

The single biggest iteration-count reducer. Three additions:

### `grep`

- **Args:** `pattern` (regex), `path` (default `.`), `glob` (file filter,
  e.g. `*.py`), `context_lines` (default 2), `max_matches` (default 50,
  ceiling 200), `case_sensitive` (default false).
- **Impl:** try `rg --json` inside the Cell first (`rg` baked into the Docker
  image; detected once per Cell via `command -v rg`); fall back to a pure-
  Python walker (`re` over files matched by the glob, binary-sniff and skip,
  `.git`/`node_modules`/`.venv`/`.forge_spill` pruned). Both paths return the
  identical rendered shape:
  `path:line:  matched line` with `context_lines` of surround, match count,
  and a `…capped at {max_matches}` notice.
- **Flags:** `is_read_only = True`, `is_concurrency_safe = True`,
  `max_result_chars = 40_000`.
- **Why not "just run rg via run_command":** structured caps, guaranteed
  availability (fallback), parallel-safety declaration, and pruned noise —
  the model gets a reliable instrument instead of a maybe-installed binary.

### `glob`

- **Args:** `pattern` (e.g. `src/**/*.ts`), `path` (default `.`).
- **Impl:** `pathlib` glob inside the workspace, pruned dirs as above, sorted
  by mtime descending (recently-touched first — the same heuristic Claude
  Code uses), capped at 200 entries. Read-only, concurrency-safe.

### `read_file` gains ranges + line numbers (breaking-internal, additive-external)

- **Args add:** `offset` (1-based start line, optional), `limit` (line count,
  optional, default 2000 when the file exceeds it).
- **Output becomes line-numbered** (`   412→ content`) — this is what makes
  `edit_file`'s exact-match anchoring reliable on big files, and it composes
  with `grep`'s `path:line` output ("grep found it at line 812 → read
  offset=790 limit=60").
- Freshness note: a **ranged** read records the hash of the **full** file in
  `FileStateCache` (one extra full read server-side, invisible to the model)
  so edit-grounding still covers the whole file. The 200 K-char cap now
  applies per-read, not per-file — any file becomes workable in windows.

**Tests** (`test_search_tools.py`): rg-present and rg-absent parity on a
fixture tree; prune list; caps; ranged read numbering; freshness-after-
ranged-read edit round-trip.

---

## W8 — Background processes (W8a Cell surface + W8b tools)

**Problem.** `Cell.run` blocks with a ≤600 s ceiling. Dev servers, watchers,
and long builds are impossible — which forbids the run-and-verify discipline
that defines a real execution peer.

### W8a — Cell contract (`cell/base.py` + both backends)

Four methods and a table, added to the ABC:

```python
async def spawn(self, command: str, env: ...) -> str            # → proc_id
async def poll(self, proc_id: str, tail_bytes: int = 20_000) -> ProcStatus
async def kill(self, proc_id: str) -> bool
def processes(self) -> list[ProcSummary]                        # for teardown + status

@dataclass(frozen=True)
class ProcStatus:
    running: bool
    exit_code: int | None
    stdout_tail: str        # last N bytes, from the spool file
    stderr_tail: str
    runtime_s: float
```

- Output spools to `.forge_spill/proc_{id}.{out,err}.txt` from the moment of
  spawn (reusing W5's spill convention — `poll` reads tails from disk, and
  the model can `grep` the spool like any file).
- **SubprocessCell:** `create_subprocess_shell` with `start_new_session=True`
  (kill the whole process group on `kill`/`close`); spool via file handles
  passed as stdout/stderr.
- **DockerCell:** `docker exec -d` with output redirected to the spool paths
  inside the container; `kill` via `docker exec kill -TERM -{pgid}`; the
  container's death (Cell `close`) reaps everything regardless.
- **Policy:** `max_background_procs = 5` on `CellPolicy`; `close()` kills the
  table — **nothing outlives the job.** A background process is job-scoped by
  definition; anything meant to outlive the job is a deployment, and that is
  Mark VI system_ops territory, not the Cell's.

### W8b — Tools (`tools/process.py`, new)

- **`start_process`** — spawn, return `proc_id` + spool paths. Not read-only,
  not concurrency-safe. Gate note: the command string passes through the same
  `_DESTRUCTIVE_CMD_PATTERNS` scan as `run_command` (`gate_reason` already
  keys on the `command` arg name — free).
- **`check_process`** — poll one (`proc_id`) or list all. Read-only,
  concurrency-safe. Description teaches the pattern: *start server → sleep
  via `run_command sleep 2` → check_process → curl → iterate*.
- **`stop_process`** — kill. Not concurrency-safe.

**Tests** (`test_process_tools.py`): spawn/poll/kill round-trip on both
backends; spool tail correctness; table cap; `close()` reaps; group-kill
(child-of-child dies too).

---

## W9 — Web tools (`forge/tools/web.py`, new)

**Problem.** `allow_network` exists as policy but no tool benefits; a coding
peer that cannot read the docs of the library it is using is handicapped.

- **`web_fetch`** — Args: `url`, `max_chars` (default 20 000). GET with
  redirects (≤5), 15 s timeout, html→text reduction (strip
  script/style/nav → text with links preserved as `[text](url)`; a
  ~60-line stdlib `HTMLParser` subclass, no bs4 dependency), then the normal
  spill path for oversize. Read-only, concurrency-safe.
- **`web_search`** — thin adapter behind `FORGE_SEARCH_URL` +
  `FORGE_SEARCH_KEY` (Brave-shaped by default, matching the key already used
  Mark VI-side). Returns title/url/snippet ×10. **Registered only when the
  key is configured** — same absent-not-locked convention as Hisar's deposit
  route.
- **Permission model:** both tools hard-check `ctx.network_allowed` and return
  a clear is_error when the job was dispatched without network — the model
  learns to request it rather than silently failing. They run **Warden-side**
  (host process), not in the Cell: the Cell's no-network posture is about
  *generated code*, not the harness's own instruments — same separation the
  Graphify sidecar already uses.
- `task_dispatch` from Mark VI currently pins `network=False`; Mark VI's
  `dispatch_agent` skill should grow a `network: bool` arg so SPEDA can grant
  it per-task (one-line schema addition on the Mark VI side, noted in
  TRUST §5's counterpart list).

**Tests:** reduction fixture (html → expected text), size cap/spill, the
network-denied error shape, search adapter parsing.

---

## W10 — Task list (`forge/tools/tasks.py`, new)

**Problem.** 20+-iteration jobs drop steps; compaction (W2) makes it worse —
a summarized plan is a lossy plan.

**Design.** Harness-held state, one tool:

- **`task_list`** — Args: `action` ∈ `set` (replace full list) | `check`
  (mark item done) | `show`; `items: list[str]` for `set`, `index` for
  `check`. State lives on `ToolContext` (a `TaskListState` dataclass), never
  in the transcript.
- **The compaction hook is the point:** W2's summary prompt receives the
  current task list verbatim and re-injects it *outside* the summarized body
  — the plan survives compaction losslessly even when everything else is
  summarized.
- Every mutation emits a `task_update` JobEvent → Heartbreaker can render a
  live checklist for dispatched jobs later (event is additive; Mark VI's
  proxy drops unknown types harmlessly today).
- Description instructs: use for jobs with ≥3 distinct steps; check items
  immediately on completion; keep items concrete.

**Tests:** state transitions, compaction-survival integration test (with W2).

---

## W11 — Explore sub-loop (`forge/tools/explore.py`, new)

**Problem.** Answering "how does auth work in this repo" costs the main
transcript 15 tool results of dead-end reads that compaction later has to
summarize away. Claude Code solves this with subagents; Forge gets the
minimal version.

**Design.**

- **`explore`** — Args: `question`, `max_iterations` (default 12, ceiling 20).
- Spawns a **fresh Warden** with: read-only toolset (`read_file`, `grep`,
  `glob`, graph tools), the **same Cell** (safe — everything is read-only),
  `Mode.PLAN` permissions (belt over suspenders: mutations deny), same model,
  a purpose-built system prompt ("investigate and answer; end with a compact
  report citing paths and line numbers"), and its **own LoopState** — zero
  context shared with the parent.
- Parent pays: one tool call + one report (≤8 000 chars). The sub-loop's
  transcript is discarded.
- Parent's `signal` is passed through — operator interrupt reaches the child.
- Recursion guard: the explore toolset does not include `explore`.
- Not concurrency-safe in Mark II (two sub-loops sharing one model client and
  cell is fine in principle; sequential keeps reasoning simple — revisit).

**Tests:** scripted-model sub-loop returns report; mutation attempt inside
explore denied; interrupt propagation; recursion absence.

---

## W12 — Git discipline (prompt + one tool + gate tuning)

**Problem.** The peer scaffolds projects with no VCS hygiene: no init, no
commits, dirty trees handed back. The gate blocks catastrophes but nothing
*teaches* the workflow.

**Design — three small pieces:**

1. **Shared prompt section** (`forge/agents/` shared fragment, appended to
   coding profiles' system prompts): initialize a repo for any new project;
   commit at meaningful checkpoints with imperative one-line messages +
   `Forged-by: {agent}` trailer; never commit secrets/venvs/node_modules
   (write `.gitignore` first); never push unless the task says to; leave the
   tree clean at job end.
2. **`git_status`** tool — porcelain status + current branch + last 3
   subjects, read-only, concurrency-safe. One reliable instrument instead of
   three shell round-trips; everything else (add/commit/diff) stays
   `run_command` — no wrapper zoo.
3. **Gate tuning** (`permissions.py`): the sensitive-path rule
   `(^|/)\.git/` currently also catches *reads* of `.git/config` etc. via
   path-carrying tools; scope the pattern to **mutating** tools only
   (`is_read_only` check before the path scan) so `git_status`-adjacent
   reads don't trip it. Force-push/reset-hard/clean patterns stay exactly as
   they are — W13's ask-flow becomes their escape hatch.

**Interaction with Hisar plan:** the archived-scaffold step
(`HISAR_FORGE_PLACEMENT_PLAN.md` H4) excludes `.git` when copying to
`Forge/projects/` — commits stay in the live workspace, the archive stays
lean. Unchanged, just noted.

**Tests:** prompt fragment presence per profile; `git_status` rendering;
gate read-vs-write scoping.

---

## Config summary

| Var | Default | Workstream |
|---|---|---|
| `FORGE_SEARCH_URL` / `FORGE_SEARCH_KEY` | unset (tool absent) | W9 |
| `CellPolicy.max_background_procs` | `5` | W8 |
| (none for W7/W10/W11/W12 — behavior is fixed) | | |

## Profile allowlist changes (`forge/agents/`)

`CODING_TOOLS` grows: `grep`, `glob`, `start_process`, `check_process`,
`stop_process`, `task_list`, `explore`, `git_status`, `web_fetch`
(+ `web_search` when configured). Centurion's security profile picks the same
navigation set; its offensive-tooling additions are out of Mark II scope.
