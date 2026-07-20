"""Navigation: `grep` and `glob`.

The single largest iteration-count reducer in the toolbelt. Without them the only
way to look at a repo is to read whole files, which is how a context window fills
before the work starts.

Two deliberate choices:

**Pure Python, no ripgrep.** The reference harness builds Grep on `rg` because it
ships a binary to millions of machines and can guarantee it. The Forge cannot —
`rg` is absent from the Cell image and from most operator hosts — and a tool that
is fast when a binary happens to exist and different when it does not costs a
parity test suite to keep two engines honest. One engine that always behaves
identically is worth more here than one that is occasionally faster. `re` over a
pruned tree is comfortably fast at repo scale; if that stops being true, `rg`
slots in behind this same interface as an optimization, not a second contract.

**Warden-side, not in the Cell.** Search reads the workspace through
`cell.host_path` — the same separation the Graphify sidecar already uses. The
Cell's isolation posture governs code the model *writes and runs*, not the
harness's own instruments. A backend with no host-visible workspace gets a clear
is_error instead of a wrong answer.
"""
from __future__ import annotations

import asyncio
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Literal

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult

# Directories that are never what the question meant. Pruned during the walk, so
# their contents cost nothing rather than being read and discarded.
PRUNED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build",
    ".next", ".nuxt", "target", ".gradle", ".idea", ".vscode", ".forge_spill",
})

_BINARY_SNIFF_BYTES = 8192
_MAX_FILE_BYTES = 5_000_000        # a source file this large is generated; skip it


def _no_workspace() -> ToolResult:
    return ToolResult(
        "This Cell has no host-visible workspace, so search is unavailable. Use "
        "run_command with the tools available inside the Cell instead.",
        is_error=True)


@lru_cache(maxsize=128)
def _compile_glob(pattern: str) -> tuple["re.Pattern", bool]:
    """Compile a glob to `(matcher, match_basename_only)`.

    `*` and `?` stop at a path separator, `**` crosses them — both tools share
    this so `src/**/*.ts` means the same thing to each.

    A pattern with no separator matches the file *name* at any depth: `*.py`
    means "python files", not "python files in the root". That is what gitignore
    and ripgrep do, and it is what a caller means."""
    out = ["\\A"]
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")       # zero or more directories
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    out.append("\\Z")
    return re.compile("".join(out)), "/" not in pattern


def _walk(root: Path, glob: str | None) -> Iterator[tuple[Path, str]]:
    """Yield (path, workspace-relative posix path) for candidate files under
    `root`, pruning noise directories *during* the walk — pruning after the fact
    still pays to descend into node_modules."""
    matcher = basename_only = None
    if glob is not None:
        matcher, basename_only = _compile_glob(glob)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in PRUNED_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if matcher is not None and not matcher.match(name if basename_only else rel):
                continue
            yield path, rel


def _readable_text(path: Path) -> str | None:
    """The file's text, or None if it is binary, unreadable, or absurdly large."""
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        with path.open("rb") as fh:
            head = fh.read(_BINARY_SNIFF_BYTES)
        if b"\x00" in head:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


# ── grep ─────────────────────────────────────────────────────────────────────
class GrepArgs(BaseModel):
    pattern: str = Field(description="Regular expression to search for.")
    path: str = Field(default=".", description="Workspace-relative directory to search under.")
    glob: str | None = Field(
        default=None,
        description="Filter files by glob, e.g. '*.py' or 'src/**/*.ts'. Omit to search all.")
    output_mode: Literal["files_with_matches", "content", "count"] = Field(
        default="files_with_matches",
        description="'files_with_matches' lists paths (cheapest, best for orienting); "
                    "'content' shows matching lines; 'count' shows per-file match counts.")
    context_lines: int = Field(default=0, ge=0, le=20,
                               description="Lines of surrounding context. content mode only.")
    max_matches: int = Field(default=50, ge=1, le=500,
                             description="Stop after this many matches or files.")
    case_sensitive: bool = Field(default=False, description="Match case exactly.")
    multiline: bool = Field(
        default=False,
        description="Let '.' match newlines, so a pattern like 'class Foo.*?def bar' "
                    "can span lines. Character classes such as [\\s\\S] already span "
                    "lines without this.")


class Grep(Tool):
    name = "grep"
    description = (
        "Search file contents across the workspace with a regular expression. Always "
        "prefer this over running grep or rg through run_command: it prunes noise "
        "directories, skips binaries, caps its own output, and is safe to run in "
        "parallel with other reads. Start in the default 'files_with_matches' mode to "
        "find where something lives, then switch to 'content' mode or read the specific "
        "lines — content mode over a broad pattern is the expensive way to orient."
    )
    Args = GrepArgs
    is_read_only = True
    is_concurrency_safe = True
    max_result_chars = 40_000

    async def call(self, args: GrepArgs, ctx: ToolContext) -> ToolResult:
        root = ctx.cell.host_path
        if root is None:
            return _no_workspace()
        try:
            flags = re.MULTILINE | (0 if args.case_sensitive else re.IGNORECASE)
            if args.multiline:
                flags |= re.DOTALL
            rx = re.compile(args.pattern, flags)
        except re.error as e:
            return ToolResult(f"Invalid regular expression {args.pattern!r}: {e}", is_error=True)

        base = (root / args.path).resolve()
        if not base.is_dir():
            return ToolResult(f"Not a directory in the workspace: {args.path}", is_error=True)
        # os.walk + re over a repo is blocking work; keep it off the event loop so
        # a parallel batch of searches actually overlaps.
        return await asyncio.to_thread(self._search, rx, root, base, args)

    def _search(self, rx: re.Pattern, root: Path, base: Path, args: GrepArgs) -> ToolResult:
        lines_out: list[str] = []
        files_hit = 0
        total = 0
        capped = False

        for path, _ in _walk(base, args.glob):
            text = _readable_text(path)
            if text is None:
                continue
            hits = list(rx.finditer(text))
            if not hits:
                continue
            files_hit += 1
            rel = path.relative_to(root).as_posix()

            if args.output_mode == "files_with_matches":
                lines_out.append(rel)
                total += 1
            elif args.output_mode == "count":
                lines_out.append(f"{rel}: {len(hits)}")
                total += 1
            else:
                body = text.splitlines()
                for hit in hits:
                    if total >= args.max_matches:
                        break
                    # Line number of the match START — with multiline patterns the
                    # match may run past it, and the start is what the reader wants.
                    line_no = text.count("\n", 0, hit.start()) + 1
                    lo = max(1, line_no - args.context_lines)
                    hi = min(len(body), line_no + args.context_lines)
                    for n in range(lo, hi + 1):
                        sep = ":" if n == line_no else "-"
                        lines_out.append(f"{rel}{sep}{n}{sep} {body[n - 1]}")
                    total += 1
                    if args.context_lines:
                        lines_out.append("--")

            if total >= args.max_matches:
                capped = True
                break

        if not lines_out:
            return ToolResult(f"No matches for {args.pattern!r}"
                              + (f" in files matching {args.glob!r}" if args.glob else "")
                              + ".")

        header = (f"{total} match{'' if total == 1 else 'es'}"
                  if args.output_mode == "content"
                  else f"{files_hit} file{'' if files_hit == 1 else 's'}")
        note = (f"\n…[capped at {args.max_matches}; narrow the pattern, set a glob, "
                f"or raise max_matches]" if capped else "")
        return ToolResult(f"{header} for {args.pattern!r}:\n" + "\n".join(lines_out) + note)


# ── glob ─────────────────────────────────────────────────────────────────────
class GlobArgs(BaseModel):
    pattern: str = Field(description="Glob pattern, e.g. 'src/**/*.ts' or '*.md'.")
    path: str = Field(default=".", description="Workspace-relative directory to search under.")


class Glob(Tool):
    name = "glob"
    description = (
        "Find files by name pattern anywhere in the workspace, including '**' for "
        "recursive matches. Results come back most-recently-modified first, which is "
        "usually the order you want when orienting in an unfamiliar repo. Read-only and "
        "safe to run in parallel. Use grep instead when you care about file contents."
    )
    Args = GlobArgs
    is_read_only = True
    is_concurrency_safe = True
    max_result_chars = 20_000

    MAX_RESULTS = 200

    async def call(self, args: GlobArgs, ctx: ToolContext) -> ToolResult:
        root = ctx.cell.host_path
        if root is None:
            return _no_workspace()
        base = (root / args.path).resolve()
        if not base.is_dir():
            return ToolResult(f"Not a directory in the workspace: {args.path}", is_error=True)
        return await asyncio.to_thread(self._glob, root, base, args.pattern)

    def _glob(self, root: Path, base: Path, pattern: str) -> ToolResult:
        found: list[tuple[float, str]] = []
        for path, _ in _walk(base, pattern):
            try:
                found.append((path.stat().st_mtime, path.relative_to(root).as_posix()))
            except OSError:
                continue

        if not found:
            return ToolResult(f"No files match {pattern!r}.")
        found.sort(key=lambda pair: pair[0], reverse=True)
        shown = found[:self.MAX_RESULTS]
        note = (f"\n…[{len(found) - len(shown)} more; narrow the pattern]"
                if len(found) > len(shown) else "")
        return ToolResult(f"{len(found)} file{'' if len(found) == 1 else 's'} "
                          f"matching {pattern!r}, most recently modified first:\n"
                          + "\n".join(rel for _, rel in shown) + note)
