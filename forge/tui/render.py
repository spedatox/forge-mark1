"""Turning the engine's event stream into something a person can read.

The engine already emits everything the loop knows — chunk, tool, tool_result,
usage, compact, error. This subscribes to that same stream, which means the TUI
watches a job exactly the way Mark VI does over the socket. There is no second
path and no privileged access: if something is invisible here it is invisible to
Heartbreaker too, and that is a bug in the event vocabulary rather than in the
renderer.

The distinction the layout exists to preserve: what the model **said** is plain
text at the margin, and everything the harness **did** is indented and dim. A
transcript that blurs those reads like the model ran the commands itself.
"""
from __future__ import annotations

import json
from typing import Any

from forge.tui import ansi


class StreamRenderer:
    """Consumes JobEvents for one turn and writes the terminal view."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self._wrote_text = False        # has model prose landed this turn?
        self._tool_names: dict[str, str] = {}

    async def __call__(self, event: Any) -> None:
        kind = getattr(event, "type", None) or event.get("type")
        data = getattr(event, "data", None) if hasattr(event, "type") else event.get("data")
        handler = getattr(self, f"_on_{kind}", None)
        if handler is not None:
            handler(data)

    # ── The model's own output ───────────────────────────────────────────────
    def _on_chunk(self, data: Any) -> None:
        text = str(data or "")
        if not text:
            return
        if not self._wrote_text:
            ansi.write()
            self._wrote_text = True
        # No newline: deltas arrive mid-word and the point of streaming is that
        # it appears at the speed it is produced.
        ansi.write(text, end="")

    def _on_done(self, data: Any) -> None:
        if self._wrote_text:
            ansi.write()

    # ── What the harness did ─────────────────────────────────────────────────
    def _on_tool(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        name = str(data.get("name", "?"))
        self._tool_names[str(data.get("id"))] = name
        args = data.get("input") or {}
        ansi.write()
        ansi.write(ansi.paint("  ⏺ ", "cyan") + ansi.paint(name, "bold", "cyan")
                   + "  " + ansi.paint(_summarize_args(args), "grey"))

    def _on_tool_result(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        content = str(data.get("content", ""))
        failed = bool(data.get("is_error"))
        marker = ansi.paint("    ✗ ", "red") if failed else ansi.paint("    ↳ ", "grey")

        if self.verbose:
            body = content
        else:
            # One line by default. The full result is in the transcript and the
            # spill file; a terminal that dumps 40K of grep output buries the
            # conversation it is supposed to be showing.
            body = ansi.truncate(content, max(20, ansi.terminal_width() - 10))
        style = "red" if failed else "grey"
        for i, line in enumerate(body.splitlines() or [""]):
            ansi.write((marker if i == 0 else "      ") + ansi.paint(line, style))

    # ── Harness housekeeping the operator should still see ───────────────────
    def _on_compact(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        stage = data.get("stage")
        if stage == "elide":
            note = f"reclaimed {data.get('freed_chars', 0):,} chars of old tool output"
        elif stage == "summarize":
            note = "summarizing the conversation so far…"
        else:
            note = f"compacted to {data.get('messages', '?')} messages"
        ansi.write(ansi.paint(f"  ◆ context: {note}", "magenta"))

    def _on_error(self, data: Any) -> None:
        ansi.write()
        ansi.write(ansi.paint(f"  ✗ {data}", "red"))

    def _on_usage(self, data: Any) -> None:
        if self.verbose and isinstance(data, dict):
            ansi.write(ansi.paint(
                f"  · {data.get('prompt', 0):,} in / {data.get('output', 0):,} out"
                f"  ({int(100 * data.get('fullness', 0))}% full)", "dim"))


def _summarize_args(args: dict[str, Any]) -> str:
    """The one detail that says what a call will actually do.

    A tool row is only useful if it names the target — `run_command` tells you
    nothing, `run_command  pytest -q` tells you everything. The argument that
    matters is nearly always the first of command/path/pattern."""
    for key in ("command", "path", "pattern", "url", "name", "question"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return ansi.truncate(value, max(20, ansi.terminal_width() - 24))
    if not args:
        return ""
    try:
        return ansi.truncate(json.dumps(args, default=str), 60)
    except (TypeError, ValueError):
        return ""


def banner(agent: str, model: str, workspace: str, tools: int) -> str:
    return "\n".join([
        "",
        ansi.paint("  ▲ FORGE", "bold", "cyan") + ansi.paint(f"  {agent}", "cyan"),
        ansi.paint(f"    {model}", "grey"),
        ansi.paint(f"    {workspace}", "grey"),
        ansi.paint(f"    {tools} tools · /help for commands", "dim"),
        "",
    ])
