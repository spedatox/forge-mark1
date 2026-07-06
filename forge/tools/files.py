"""File tools operating inside the Cell, with read-before-write grounding.

Freshness is tracked by content hash rather than mtime — cross-platform and
robust (the study notes mtime is unreliable on Windows / cloud-sync, so it falls
back to a content compare; the Forge uses the content compare as the primary
signal)."""
from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()


# ── read_file ────────────────────────────────────────────────────────────────
class ReadFileArgs(BaseModel):
    path: str = Field(description="Workspace-relative path of the file to read.")


class ReadFile(Tool):
    name = "read_file"
    description = (
        "Read a UTF-8 text file from your sandbox workspace and return its contents. "
        "Use this before editing a file — edits are rejected until you have read the "
        "current version (read-before-write). This tool is read-only and safe to run "
        "in parallel with other reads. It returns the full file text."
    )
    Args = ReadFileArgs
    is_read_only = True
    is_concurrency_safe = True
    max_result_chars = 200_000     # a file is context the model asked for; spill only if huge

    async def call(self, args: ReadFileArgs, ctx: ToolContext) -> ToolResult:
        try:
            content = await ctx.cell.read(args.path)
        except FileNotFoundError as e:
            return ToolResult(f"File not found: {args.path} ({e})", is_error=True)
        ctx.files.record(args.path, content, _hash(content))
        return ToolResult(content)


# ── write_file ───────────────────────────────────────────────────────────────
class WriteFileArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to write (created or overwritten).")
    content: str = Field(description="Full UTF-8 contents to write.")


class WriteFile(Tool):
    name = "write_file"
    description = (
        "Create a new file or completely overwrite an existing one with the given "
        "contents, inside your sandbox workspace. Use this for brand-new files or full "
        "rewrites; use edit_file for a targeted change to a file you have read. It is a "
        "mutating operation and runs one at a time. Writing to protected locations "
        "(.git internals, credentials, shell config) is stopped by the safety gate."
    )
    Args = WriteFileArgs
    is_read_only = False
    is_concurrency_safe = False

    async def call(self, args: WriteFileArgs, ctx: ToolContext) -> ToolResult:
        await ctx.cell.write(args.path, args.content)
        ctx.files.record(args.path, args.content, _hash(args.content))
        return ToolResult(f"Wrote {len(args.content)} chars to {args.path}.")


# ── edit_file ────────────────────────────────────────────────────────────────
class EditFileArgs(BaseModel):
    path: str = Field(description="Workspace-relative path of the file to edit.")
    old_string: str = Field(description="Exact text to replace. Must appear exactly once.")
    new_string: str = Field(description="Replacement text.")


class EditFile(Tool):
    name = "edit_file"
    description = (
        "Make a targeted edit to a file by replacing an exact substring. You must have "
        "read the file first and it must not have changed since (read-before-write "
        "grounding), or the edit is rejected with an explanation. The old_string must "
        "match exactly once so the edit is unambiguous. It is a mutating operation and "
        "runs one at a time."
    )
    Args = EditFileArgs
    is_read_only = False
    is_concurrency_safe = False

    async def call(self, args: EditFileArgs, ctx: ToolContext) -> ToolResult:
        try:
            current = await ctx.cell.read(args.path)
        except FileNotFoundError:
            return ToolResult(f"File {args.path!r} does not exist. Create it with write_file.",
                              is_error=True)

        err = ctx.files.freshness_error(args.path, _hash(current))
        if err:
            return ToolResult(err, is_error=True)

        occurrences = current.count(args.old_string)
        if occurrences == 0:
            return ToolResult(f"old_string not found in {args.path}. Read the file and retry.",
                              is_error=True)
        if occurrences > 1:
            return ToolResult(
                f"old_string appears {occurrences} times in {args.path}; it must be unique. "
                f"Include more surrounding context.", is_error=True)

        updated = current.replace(args.old_string, args.new_string)
        await ctx.cell.write(args.path, updated)
        ctx.files.record(args.path, updated, _hash(updated))
        return ToolResult(f"Edited {args.path} (1 replacement).")
