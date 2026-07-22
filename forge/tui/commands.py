"""Slash commands — a registry, like every other extension point in Forge.

The reference harness has 101 of these. Forge has the dozen that answer a
question you cannot otherwise answer from inside a session: what is this costing,
how full is the window, what can the agent reach, what did the operator already
approve.

Each command returns a `CommandResult` rather than printing. That keeps them
testable without a terminal, and it is what lets `/compact` hand work back to the
session instead of reaching into it — a command that printed would have to know
about rendering, and a command that mutated state directly would have to know
about the loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from forge.tui.session import Session


@dataclass
class CommandResult:
    """What a command wants done. `text` is shown; the flags ask the session for
    something only the session can do."""
    text: str = ""
    quit: bool = False
    clear: bool = False
    compact: bool = False


@dataclass
class Command:
    name: str
    summary: str                 # one line, shown by /help
    run: "Callable[[str, Session], Awaitable[CommandResult]]"
    aliases: tuple[str, ...] = ()


REGISTRY: dict[str, Command] = {}


def register(command: Command) -> Command:
    """Add a command. Later registration wins for a name — the registry is
    ordinary state, and a caller replacing a builtin has said what they meant."""
    REGISTRY[command.name] = command
    for alias in command.aliases:
        REGISTRY[alias] = command
    return command


def resolve(line: str) -> "tuple[Command | None, str]":
    """Split `/name rest` into its command and argument string."""
    body = line[1:].strip()
    if not body:
        return None, ""
    name, _, rest = body.partition(" ")
    return REGISTRY.get(name.lower()), rest.strip()


def command(name: str, summary: str, *aliases: str):
    def wrap(fn):
        register(Command(name=name, summary=summary, run=fn, aliases=aliases))
        return fn
    return wrap


# ── The commands ─────────────────────────────────────────────────────────────
@command("help", "list these commands", "?", "h")
async def _help(args: str, session: "Session") -> CommandResult:
    seen: dict[str, Command] = {}
    for cmd in REGISTRY.values():
        seen[cmd.name] = cmd
    width = max(len(c.name) for c in seen.values()) + 2
    lines = [f"  /{c.name:<{width}}{c.summary}" for c in sorted(seen.values(), key=lambda c: c.name)]
    return CommandResult("\n".join(lines) + "\n\n  Ctrl-C interrupts a running turn; Ctrl-D exits.")


@command("exit", "leave the session", "quit", "q")
async def _exit(args: str, session: "Session") -> CommandResult:
    return CommandResult(quit=True)


@command("clear", "forget the conversation and start fresh")
async def _clear(args: str, session: "Session") -> CommandResult:
    return CommandResult(clear=True)


@command("compact", "summarize the conversation now, freeing context")
async def _compact(args: str, session: "Session") -> CommandResult:
    if not session.messages:
        return CommandResult("Nothing to compact yet.")
    return CommandResult(compact=True)


@command("cost", "what this session has spent")
async def _cost(args: str, session: "Session") -> CommandResult:
    led = session.ledger
    if not led.turns:
        return CommandResult("No model turns yet.")
    lines = [
        f"  turns          {led.turns}",
        f"  input          {led.input_tokens:,} uncached",
        f"  output         {led.output_tokens:,}",
        f"  cache read     {led.cache_read_tokens:,}",
        f"  cache written  {led.cache_write_tokens:,}",
    ]
    if led.estimated:
        # Never present a guess as a measurement.
        lines.append("\n  These are ESTIMATES — this provider does not report usage.")
    return CommandResult("\n".join(lines))


@command("context", "how full the window is, and what is in it", "ctx")
async def _context(args: str, session: "Session") -> CommandResult:
    led = session.ledger
    used, limit = led.prompt_tokens, led.effective_limit
    pct = int(100 * used / max(1, limit))
    filled = int(28 * used / max(1, limit))
    bar = "█" * min(28, filled) + "░" * max(0, 28 - filled)
    lines = [
        f"  [{bar}] {pct}%",
        f"  {used:,} of {limit:,} usable tokens"
        + ("  (estimated)" if led.estimated else ""),
        f"  compaction at  {led.compact_at:,}",
        f"  messages       {len(session.messages)}",
    ]
    if led.should_compact():
        lines.append("\n  Over the threshold — the next turn will compact first.")
    return CommandResult("\n".join(lines))


@command("tools", "what the agent can reach")
async def _tools(args: str, session: "Session") -> CommandResult:
    if not session.tools:
        return CommandResult("No tools.")
    width = max(len(n) for n in session.tools) + 2
    lines = []
    for name in sorted(session.tools):
        tool = session.tools[name]
        marks = []
        if tool.READ_ONLY:
            marks.append("read-only")
        if tool.CONCURRENCY_SAFE:
            marks.append("parallel")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        lines.append(f"  {name:<{width}}{ansi_first_sentence(tool.description)}{suffix}")
    return CommandResult("\n".join(lines))


def ansi_first_sentence(text: str, limit: int = 60) -> str:
    head = text.split(". ")[0].strip()
    return head if len(head) <= limit else head[: limit - 1] + "…"


@command("model", "which model this session uses")
async def _model(args: str, session: "Session") -> CommandResult:
    return CommandResult(f"  {session.model_ref}\n  window {session.ledger.context_limit:,} tokens")


@command("agent", "the agent profile in use")
async def _agent(args: str, session: "Session") -> CommandResult:
    cfg = session.cfg
    return CommandResult(
        f"  {cfg.agent_id} — {cfg.name}\n"
        f"  domain     {cfg.domain}\n"
        f"  mode       {cfg.permission_mode}\n"
        f"  max iters  {cfg.max_iterations}")


@command("approved", "operations you have permanently approved")
async def _approved(args: str, session: "Session") -> CommandResult:
    entries = sorted(session.allowlist.entries)
    if not entries:
        return CommandResult("Nothing approved yet. Gated operations will ask.")
    return CommandResult("\n".join(f"  {e}" for e in entries)
                         + f"\n\n  Stored in {session.allowlist.path or '(memory only)'}")


@command("transcript", "dump the raw conversation")
async def _transcript(args: str, session: "Session") -> CommandResult:
    from forge.warden.compaction import render_for_summary

    if not session.messages:
        return CommandResult("Empty.")
    return CommandResult(render_for_summary(session.messages))


@command("cwd", "which directory the agent is working in")
async def _cwd(args: str, session: "Session") -> CommandResult:
    return CommandResult(f"  {session.workspace}")
