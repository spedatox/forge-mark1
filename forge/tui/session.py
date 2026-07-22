"""One interactive session: state that outlives a turn, and how a turn renders.

The Warden runs one job and returns a Terminal. A conversation is many of those
sharing a transcript, a ledger, a Cell and a set of standing approvals — so the
session owns those and hands them to each turn, rather than a turn owning
anything.

The terminal oracle is the piece worth pointing at. It is the third
implementation of Seam 2 (after auto-deny and the peer socket), it took eleven
lines, and no core module knew it was coming. That is the whole claim the seam
was built to make.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.agents.config import AgentConfig
from forge.tui import ansi
from forge.warden.ledger import TokenLedger
from forge.warden.oracle import Answer
from forge.warden.permissions import AllowList
from forge.warden.tool import Tool


class TerminalOracle:
    """Ask the operator, who is right here.

    No timeout: the peer's oracle races a countdown because nobody may be
    watching, but here somebody demonstrably is — they just typed. A prompt that
    expired while they were reading the command would be hostile, and the answer
    to "are you still there" is that the cursor is blinking at them.

    Ctrl-C at the prompt is a refusal, not a crash: interrupting the question is
    a perfectly clear way to say no."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    async def ask(self, tool_name: str, action_key: str, reason: str) -> Answer:
        self.asked.append((tool_name, action_key))
        ansi.write()
        ansi.write(ansi.paint("  ⚠  PERMISSION", "bold", "orange") + ansi.paint(
            f"  {tool_name}", "orange"))
        ansi.write(ansi.paint(f"  {reason}", "grey"))
        ansi.write()
        # Verbatim and unwrapped: approving a force-push means seeing the branch.
        for line in action_key.splitlines() or [action_key]:
            ansi.write("    " + ansi.paint(line, "bold"))
        ansi.write()
        ansi.write(ansi.paint("    [y] once   [a] always   [n] no", "grey"))

        try:
            choice = (await asyncio.to_thread(input, "    > ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            ansi.write()
            return Answer(False, note="interrupted at the prompt")

        if choice in ("y", "yes"):
            return Answer(True)
        if choice in ("a", "always"):
            return Answer(True, remember=True)
        return Answer(False, note="declined at the prompt")


@dataclass
class Session:
    """Everything a conversation carries between turns."""

    cfg: AgentConfig
    model_ref: str
    workspace: Path
    tools: dict[str, Tool]
    ledger: TokenLedger
    allowlist: AllowList
    cell: Any = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    oracle: TerminalOracle = field(default_factory=TerminalOracle)
    turns: int = 0

    def reset(self) -> None:
        """Forget the conversation, keep the session.

        The ledger's running costs survive deliberately: `/clear` frees context,
        it does not un-spend money, and a cost display that reset would quietly
        under-report what the session actually cost."""
        self.messages = []
        self.ledger.prompt_tokens = 0
