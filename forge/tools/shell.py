"""Shell execution tool. Runs inside the Cell only (§9.3) — the Warden never
touches the host shell."""
from __future__ import annotations

import re
import shlex

from pydantic import BaseModel, Field

from forge.warden.tool import Tool, ToolContext, ToolResult

# Commands that observe and do not change anything. Kept deliberately short: this
# list decides whether a command may run alongside others and whether plan mode
# permits it, so every entry has to be one nobody would argue about. A command
# that is not on it is treated as a mutation, which costs a little parallelism
# and never costs correctness.
READ_ONLY_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "wc", "file", "stat", "du", "df", "tree",
    "pwd", "whoami", "id", "date", "uname", "hostname", "env", "printenv",
    "which", "type", "echo", "basename", "dirname", "realpath",
    "grep", "egrep", "fgrep", "rg", "find", "locate", "diff", "cmp", "sort",
    "uniq", "cut", "awk", "sed", "jq", "md5sum", "sha256sum",
    "python", "python3", "node",       # only with an inspection flag, see below
    "pip", "npm", "cargo", "go",       # only with an inspection subcommand
    "git",                             # only with an inspection subcommand
})

# For commands that are read-only in some moods and not others, the deciding
# token. `git status` observes; `git push` does not.
_READ_ONLY_SUBCOMMANDS = {
    "git": {"status", "log", "diff", "show", "branch", "remote", "describe",
            "rev-parse", "blame", "shortlog", "config", "ls-files", "tag"},
    "pip": {"list", "show", "freeze", "check"},
    "npm": {"list", "ls", "view", "outdated"},
    "cargo": {"tree", "metadata"},
    "go": {"list", "env", "version"},
}

# An inspection flag makes an interpreter read-only; anything else runs code.
_READ_ONLY_FLAGS = {"--version", "-V", "--help", "-h"}

# Shell metacharacters that redirect or chain. Any of these and the command is
# doing more than the first token admits, so it is not classified at all.
_WRITES = re.compile(r"[>]|>>|\btee\b|\bdd\b")
_SEPARATORS = re.compile(r"[;&|]|\$\(|`")


def is_read_only_command(command: str) -> bool:
    """Best-effort, fail-closed: True only when the command is plainly harmless.

    Every ambiguity resolves to False. Redirection, chaining, substitution, an
    unparseable string, an unrecognized program — all mean "assume it writes".
    The cost of a false negative is a lost parallel slot; the cost of a false
    positive is two mutations racing on one workspace, so the asymmetry decides
    every judgement call here."""
    text = command.strip()
    if not text or _WRITES.search(text) or _SEPARATORS.search(text):
        return False
    try:
        tokens = shlex.split(text)
    except ValueError:                      # unbalanced quotes
        return False
    if not tokens:
        return False

    program = tokens[0].rsplit("/", 1)[-1].removesuffix(".exe")
    if program not in READ_ONLY_COMMANDS:
        return False

    rest = [t for t in tokens[1:] if t not in ("--",)]
    subcommands = _READ_ONLY_SUBCOMMANDS.get(program)
    if subcommands is not None:
        first = next((t for t in rest if not t.startswith("-")), None)
        # `git` alone prints usage; `git status` observes; `git push` does not.
        return first is None or first in subcommands
    if program in {"python", "python3", "node"}:
        # An interpreter is only safe when it is not running anything.
        return bool(rest) and all(t in _READ_ONLY_FLAGS for t in rest)
    if program == "find":
        # find is read-only until -delete or -exec turns it into anything at all.
        return not any(t in {"-delete", "-exec", "-execdir", "-ok", "-fprint"} for t in rest)
    if program == "sed":
        return "-i" not in rest and not any(t.startswith("-i") for t in rest)
    return True


class RunCommandArgs(BaseModel):
    command: str = Field(description="The shell command to run inside the sandbox.")
    timeout: int | None = Field(
        default=None, description="Optional per-command wall-clock timeout in seconds.")


class RunCommand(Tool):
    name = "run_command"
    description = (
        "Run a shell command inside your isolated sandbox and get back its stdout, "
        "stderr, and exit code. Use this to build, run tests, execute scripts, install "
        "packages, or inspect the environment. It is NOT read-only and NOT safe to run "
        "in parallel — commands can mutate the workspace, so they run one at a time. "
        "High-blast-radius commands (force-push, recursive delete, piping a download to "
        "a shell) are stopped by the safety gate unless the operator allow-lists them."
    )
    Args = RunCommandArgs
    READ_ONLY = False
    CONCURRENCY_SAFE = False
    DESTRUCTIVE = False       # individual dangerous commands are caught by the gate

    def is_read_only(self, args: RunCommandArgs) -> bool:
        """Whether this particular command only observes.

        A shell tool that answers for its worst case makes every `git status`
        as expensive as an `rm -rf`: it serializes behind other work and plan
        mode refuses it. Answering per command is what lets a batch of
        inspections actually be a batch."""
        return is_read_only_command(args.command)

    def is_concurrency_safe(self, args: RunCommandArgs) -> bool:
        # Two observations cannot interfere. Anything that might write shares
        # one workspace with everything else and runs alone.
        return is_read_only_command(args.command)

    async def call(self, args: RunCommandArgs, ctx: ToolContext) -> ToolResult:
        res = await ctx.cell.run(args.command, timeout=args.timeout,
                                 env={} if ctx.network_allowed else None)
        parts = [f"exit_code: {res.exit_code}"]
        if res.timed_out:
            parts.append("(timed out)")
        if res.stdout:
            parts.append(f"stdout:\n{res.stdout}")
        if res.stderr:
            parts.append(f"stderr:\n{res.stderr}")
        body = "\n".join(parts)
        return ToolResult(body, is_error=res.exit_code != 0)
