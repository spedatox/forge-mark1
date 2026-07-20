"""Navigation: grep, glob, and the ranged read (H1).

These run against a real workspace through a real SubprocessCell — the point of
the tools is what they do to a directory tree, so a fake cell would test nothing.
"""
import asyncio

import pytest

from forge.cell.base import CellPolicy
from forge.cell.subprocess_cell import SubprocessCell
from forge.tools.files import FILE_UNCHANGED_STUB, EditFile, EditFileArgs, ReadFile, ReadFileArgs
from forge.tools.search import Glob, GlobArgs, Grep, GrepArgs
from forge.warden.filestate import FileStateCache
from forge.warden.permissions import PermissionEngine
from forge.warden.tool import ToolContext

TREE = {
    "src/app.py": "import os\n\n\ndef add(a, b):\n    return a + b\n",
    "src/util.py": "def helper():\n    return 'add'\n",
    "docs/README.md": "# Notes\nCall add() to add things.\n",
    "node_modules/dep/index.js": "function add() { return 1 }\n",
    ".git/config": "[core]\n\tadd = true\n",
    "big.log": "noise\n" * 50,
}


@pytest.fixture
def ws(tmp_path):
    for rel, body in TREE.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    (tmp_path / "logo.bin").write_bytes(b"\x89PNG\x00\x00binary\x00data")
    return tmp_path


@pytest.fixture
def ctx(ws):
    cell = SubprocessCell(workspace=ws, policy=CellPolicy())
    asyncio.run(cell.start())
    return ToolContext(agent_id="t", cell=cell, graph=None, files=FileStateCache(),
                       permissions=PermissionEngine(), network_allowed=False)


def _call(tool, args, ctx):
    return asyncio.run(tool.call(args, ctx))


# ── grep ─────────────────────────────────────────────────────────────────────
def test_grep_finds_files_and_prunes_noise(ctx):
    """The default mode lists paths, and never reaches .git or node_modules."""
    out = _call(Grep(), GrepArgs(pattern=r"\badd\b"), ctx).content
    assert "src/app.py" in out and "docs/README.md" in out
    assert "node_modules" not in out
    assert ".git" not in out


def test_grep_skips_binary_files(ctx):
    out = _call(Grep(), GrepArgs(pattern="binary"), ctx).content
    assert "logo.bin" not in out


def test_grep_content_mode_reports_path_and_line(ctx):
    """content mode's shape has to compose with read_file's offset."""
    out = _call(Grep(), GrepArgs(pattern="def add", output_mode="content"), ctx).content
    assert "src/app.py:4: def add(a, b):" in out


def test_grep_context_lines(ctx):
    out = _call(Grep(), GrepArgs(pattern="def add", output_mode="content",
                                 context_lines=1), ctx).content
    assert "src/app.py-3-" in out          # the blank line above
    assert "src/app.py-5-     return a + b" in out


def test_grep_count_mode(ctx):
    out = _call(Grep(), GrepArgs(pattern="noise", path=".", glob="*.log",
                                 output_mode="count"), ctx).content
    assert "big.log: 50" in out


def test_grep_glob_filters_by_name_or_path(ctx):
    """'*.py' should work without forcing the caller to write '**/*.py'."""
    out = _call(Grep(), GrepArgs(pattern="add", glob="*.py"), ctx).content
    assert "src/app.py" in out
    assert "README.md" not in out


def test_grep_is_case_insensitive_by_default(ctx):
    assert "docs/README.md" in _call(Grep(), GrepArgs(pattern="NOTES"), ctx).content
    assert "No matches" in _call(
        Grep(), GrepArgs(pattern="NOTES", case_sensitive=True), ctx).content


def test_grep_caps_and_says_so(ctx):
    out = _call(Grep(), GrepArgs(pattern="noise", output_mode="content",
                                 max_matches=5), ctx).content
    assert "capped at 5" in out


def test_grep_reports_a_bad_regex_as_a_correctable_error(ctx):
    res = _call(Grep(), GrepArgs(pattern="a[b"), ctx)
    assert res.is_error and "Invalid regular expression" in res.content


def test_grep_multiline_controls_what_dot_matches(ctx):
    """The flag is DOTALL and nothing more: without it '.' stops at the newline."""
    assert "No matches" in _call(
        Grep(), GrepArgs(pattern=r"import os.*?def add"), ctx).content
    assert "src/app.py" in _call(
        Grep(), GrepArgs(pattern=r"import os.*?def add", multiline=True), ctx).content


def test_grep_character_classes_span_lines_without_the_flag(ctx):
    """[\\s\\S] includes the newline by construction — a caller who reaches for it
    should not also need multiline, and the description says so."""
    assert "src/app.py" in _call(
        Grep(), GrepArgs(pattern=r"import os[\s\S]*?def add"), ctx).content


# ── glob ─────────────────────────────────────────────────────────────────────
def test_glob_matches_recursively_and_prunes(ctx):
    out = _call(Glob(), GlobArgs(pattern="**/*.js"), ctx).content
    assert "No files match" in out          # the only .js is in node_modules


def test_a_bare_pattern_matches_the_name_at_any_depth(ctx):
    """gitignore/ripgrep semantics: '*.py' means "python files", not "python
    files in the root". A caller who writes it means the former."""
    out = _call(Glob(), GlobArgs(pattern="*.py"), ctx).content
    assert "src/app.py" in out and "src/util.py" in out


def test_a_pattern_with_separators_is_anchored_to_the_path(ctx):
    """'*' stops at a separator, so a one-level pattern stays one level."""
    out = _call(Glob(), GlobArgs(pattern="*/*.py"), ctx).content
    assert "src/app.py" in out
    assert _call(Glob(), GlobArgs(pattern="*/*/*.py"), ctx).content.startswith("No files")


def test_double_star_crosses_separators(ctx):
    out = _call(Glob(), GlobArgs(pattern="**/*.md"), ctx).content
    assert "docs/README.md" in out


def test_glob_orders_by_recency(ctx, ws):
    """Most-recently-modified first — the order that answers 'what changed'."""
    import os
    import time
    os.utime(ws / "src/util.py", (time.time() + 10, time.time() + 10))
    out = _call(Glob(), GlobArgs(pattern="src/*.py"), ctx).content
    lines = [ln for ln in out.splitlines() if ln.startswith("src/")]
    assert lines[0] == "src/util.py"


# ── ranged read ──────────────────────────────────────────────────────────────
def test_read_is_line_numbered(ctx):
    out = _call(ReadFile(), ReadFileArgs(path="src/app.py"), ctx).content
    assert "4→ def add(a, b):" in out


def test_read_window_starts_where_asked(ctx):
    out = _call(ReadFile(), ReadFileArgs(path="big.log", offset=10, limit=3), ctx).content
    assert out.startswith("10→ noise")
    assert "12→ noise" in out
    assert "13→" not in out


def test_read_points_at_the_rest(ctx):
    out = _call(ReadFile(), ReadFileArgs(path="big.log", limit=5), ctx).content
    assert "45 more lines" in out and "offset 6" in out


def test_read_past_the_end_is_a_correctable_error(ctx):
    res = _call(ReadFile(), ReadFileArgs(path="big.log", offset=9999), ctx)
    assert res.is_error and "past the end" in res.content


# ── the unchanged-re-read shortcut, and the trap in it ───────────────────────
def test_rereading_an_unchanged_file_returns_a_stub(ctx):
    first = _call(ReadFile(), ReadFileArgs(path="src/app.py"), ctx).content
    assert "def add" in first
    assert _call(ReadFile(), ReadFileArgs(path="src/app.py"), ctx).content == FILE_UNCHANGED_STUB


def test_the_stub_never_fires_after_a_windowed_read(ctx):
    """The model has only part of the file, so the shortcut would be a lie."""
    _call(ReadFile(), ReadFileArgs(path="big.log", limit=2), ctx)
    out = _call(ReadFile(), ReadFileArgs(path="big.log"), ctx).content
    assert out != FILE_UNCHANGED_STUB
    assert "1→ noise" in out


def test_a_changed_file_is_read_again(ctx, ws):
    _call(ReadFile(), ReadFileArgs(path="src/app.py"), ctx)
    (ws / "src/app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    assert "return a - b" in _call(ReadFile(), ReadFileArgs(path="src/app.py"), ctx).content


def test_a_windowed_read_still_grounds_an_edit_to_an_unseen_line(ctx):
    """Freshness records the hash of the WHOLE file, not the window. Editing a
    region the window never showed is allowed — but only because the file has
    not changed since. That is what stops a partial read from becoming a hole in
    read-before-write grounding."""
    _call(ReadFile(), ReadFileArgs(path="src/app.py", offset=1, limit=1), ctx)
    res = _call(EditFile(), EditFileArgs(path="src/app.py",
                                         old_string="return a + b",
                                         new_string="return a + b  # checked"), ctx)
    assert not res.is_error


def test_an_edit_after_an_outside_change_is_still_rejected(ctx, ws):
    _call(ReadFile(), ReadFileArgs(path="src/app.py", offset=1, limit=1), ctx)
    (ws / "src/app.py").write_text("wholly different\n", encoding="utf-8")
    res = _call(EditFile(), EditFileArgs(path="src/app.py", old_string="wholly",
                                         new_string="x"), ctx)
    assert res.is_error and "modified since" in res.content
