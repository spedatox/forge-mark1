"""The interactive loop — Forge's local surface.

`demo`, `serve` and `connect` all run a job somebody else asked for. This is the
one entry point where a person is present for the whole thing, which makes it
the only place several Mark II mechanisms can actually be observed: the
permission ask resolves against a human instead of a timeout, compaction happens
while you watch, and the ledger is answering a question you can ask mid-session.

**One Cell for the session, not one per turn.** `run_job` builds a fresh Cell per
job because a dispatched job is a closed unit. A conversation is not: the file
you wrote in turn three has to still be there in turn nine, and `cd` has to
survive. So the session owns the Cell and each turn borrows it.

**Ctrl-C interrupts the turn, not the session.** The engine already checks its
abort signal at both boundaries and returns a clean ABORTED terminal, so the
handler here only has to set the signal and let the loop unwind — the transcript
stays well-formed and the next prompt starts from a consistent state.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from forge.agents.registry import AgentRegistry
from forge.cell.factory import build_cell
from forge.cell.base import CellPolicy
from forge.config import ForgeSettings
from forge.extensions import load_extensions
from forge.gate.protocol import JobRequest
from forge.tui import ansi
from forge.tui.commands import resolve as resolve_command
from forge.tui.render import StreamRenderer, banner, humanize_error
from forge.tui.session import Session
from forge.warden.compaction import (
    elide_old_tool_results,
    find_cut,
    rebuild,
    render_for_summary,
    summarize,
)
from forge.warden.engine import Warden
from forge.warden.filestate import FileStateCache
from forge.warden.ledger import TokenLedger
from forge.warden.permissions import AllowList, Mode, PermissionEngine
from forge.warden.state import StopReason
from forge.warden.tool import ToolContext
from forge.warden.toolsource import close_providers, fold_providers


async def run_repl(agent: str = "optimus", workspace: Path | None = None,
                   verbose: bool = False, model_override: str | None = None) -> int:
    settings = ForgeSettings.from_env()
    registry = AgentRegistry.load()
    try:
        cfg = registry.get(agent)
    except KeyError as e:
        ansi.write(ansi.paint(f"  {e}", "red"))
        return 1

    workspace = Path(workspace or Path.cwd()).resolve()
    model_ref = model_override or cfg.model_ref
    extensions = load_extensions()
    providers = extensions.tool_providers()

    ansi.enable()
    cell = None
    try:
        cell = await build_cell(
            agent_id=cfg.agent_id, workspace_root=settings.workspace_root,
            backend=cfg.cell.backend or settings.cell_backend,
            image=cfg.cell.image or settings.cell_image,
            policy=CellPolicy(allow_network=cfg.cell.allow_network,
                              cpus=cfg.cell.cpus, memory_mb=cfg.cell.memory_mb,
                              default_timeout_s=cfg.cell.timeout_s,
                              run_as_root=cfg.cell.run_as_root,
                              cap_add=cfg.cell.cap_add),
            workspace=workspace)

        request = JobRequest(agent=cfg.agent_id, task="", repo_path=str(workspace))
        tools = await fold_providers(providers, cfg, request)

        session = Session(
            cfg=cfg, model_ref=model_ref, workspace=workspace, tools=tools, cell=cell,
            ledger=TokenLedger(context_limit=settings.context_limit,
                               max_output_tokens=settings.max_tokens),
            allowlist=AllowList.load(settings.allowlist_path))

        ansi.write(banner(f"{cfg.name} ({cfg.agent_id})", model_ref, str(workspace), len(tools)))
        return await _loop(session, settings, extensions, verbose)
    except Exception as e:  # noqa: BLE001 — a failed start should say why, not traceback
        ansi.write(ansi.paint(f"  could not start: {type(e).__name__}: {e}", "red"))
        return 1
    finally:
        await close_providers(providers)
        if cell is not None:
            await cell.close()


async def _loop(session: Session, settings: ForgeSettings, extensions, verbose: bool) -> int:
    while True:
        try:
            # input() blocks the event loop; off-thread keeps background work
            # (an MCP server settling, a spilled write) alive while we wait.
            line = (await asyncio.to_thread(input, ansi.paint("\n› ", "cyan"))).strip()
        except (EOFError, KeyboardInterrupt):
            ansi.write()
            return 0

        if not line:
            continue

        if line.startswith("/"):
            if await _run_command(line, session):
                return 0
            continue

        await _run_turn(line, session, settings, extensions, verbose)


async def _run_command(line: str, session: Session) -> bool:
    """Returns True when the session should end."""
    cmd, args = resolve_command(line)
    if cmd is None:
        ansi.write(ansi.paint(f"  unknown command: {line.split()[0]} — try /help", "yellow"))
        return False

    result = await cmd.run(args, session)
    if result.text:
        ansi.write(result.text)
    if result.clear:
        session.reset()
        ansi.write(ansi.paint("  conversation cleared", "grey"))
    if result.compact:
        await _compact_now(session)
    return result.quit


async def _compact_now(session: Session) -> None:
    """`/compact` on demand — the same two layers the engine runs itself."""
    before = len(session.messages)
    messages, freed = elide_old_tool_results(session.messages, keep_cycles=1)
    session.messages = messages
    if freed:
        ansi.write(ansi.paint(f"  ◆ reclaimed {freed:,} chars of old tool output", "magenta"))

    cut = find_cut(session.messages, keep_cycles=2)
    if cut is None:
        ansi.write(ansi.paint("  nothing further to summarize", "grey"))
        return

    ansi.write(ansi.paint("  ◆ summarizing…", "magenta"))
    model = _build_model(session)
    summary = await summarize(model, render_for_summary(session.messages[1:cut]),
                              asyncio.Event())
    if summary is None:
        ansi.write(ansi.paint("  the summary call failed; nothing was changed", "yellow"))
        return
    session.messages = rebuild(session.messages, cut, summary)
    # The model's memory of file contents is the summary's now, not the cache's.
    ansi.write(ansi.paint(
        f"  ◆ {before} messages → {len(session.messages)}", "magenta"))


def _build_model(session: Session):
    from forge.model.factory import build_model
    settings = ForgeSettings.from_env()
    return build_model(session.model_ref, settings, max_tokens=settings.max_tokens)


async def _run_turn(prompt: str, session: Session, settings: ForgeSettings,
                    extensions, verbose: bool) -> None:
    signal = asyncio.Event()
    renderer = StreamRenderer(verbose=verbose)
    files = FileStateCache()

    ctx = ToolContext(
        agent_id=session.cfg.agent_id, cell=session.cell, graph=None, files=files,
        permissions=PermissionEngine(mode=Mode(session.cfg.permission_mode),
                                     allowlist=session.allowlist),
        network_allowed=session.cfg.cell.allow_network,
        oracle=session.oracle,
        hooks=list(extensions.hooks),
    )

    warden = Warden(
        system_prompt=_system_prompt(session, extensions),
        tools=session.tools, model=_build_model(session), ctx=ctx,
        max_iterations=session.cfg.max_iterations, signal=signal,
        ledger=session.ledger,
        retry_attempts=settings.retry_attempts,
        retry_base_delay=settings.retry_base_delay_s,
        emit=renderer,
    )

    # Continue the conversation rather than starting one: seed the loop with
    # everything said so far, so turn nine remembers turn three.
    warden_task = asyncio.create_task(_drive(warden, session, prompt))
    try:
        terminal = await asyncio.shield(warden_task)
    except KeyboardInterrupt:
        # Ask the loop to stop at its next boundary. It back-fills any pending
        # tool_results so the transcript stays well-formed, then returns ABORTED.
        signal.set()
        ansi.write()
        ansi.write(ansi.paint("  ⏹ interrupting…", "yellow"))
        terminal = await warden_task
    except asyncio.CancelledError:
        signal.set()
        raise

    session.messages = list(terminal.messages)
    session.turns += 1
    _report(terminal, session, verbose, already_shown=renderer.saw_error)


async def _drive(warden: Warden, session: Session, prompt: str):
    """Run one turn over the session's accumulated transcript."""
    if session.messages:
        warden_state_messages = [*session.messages, {"role": "user", "content": prompt}]
        return await warden.run_messages(warden_state_messages)
    return await warden.run(prompt)


def _system_prompt(session: Session, extensions) -> str:
    from forge.agents.prompt import PromptFragment, compose_system_prompt
    return compose_system_prompt([
        PromptFragment("profile", session.cfg.system_prompt),
        *extensions.fragments,
    ])


def _report(terminal, session: Session, verbose: bool, already_shown: bool = False) -> None:
    if terminal.reason is StopReason.ABORTED:
        ansi.write(ansi.paint("  ⏹ stopped", "yellow"))
    elif terminal.reason is StopReason.MAX_ITERATIONS:
        ansi.write(ansi.paint(f"  ⏹ hit the {session.cfg.max_iterations}-iteration ceiling",
                              "yellow"))
    elif terminal.reason is StopReason.ERROR and not already_shown:
        # The renderer usually showed this already, via the error event. Saying
        # it twice makes one failure look like two.
        ansi.write(ansi.paint(f"  ✗ {humanize_error(terminal.error or '')}", "red"))

    if verbose or terminal.reason is not StopReason.COMPLETED:
        usage = terminal.usage or {}
        ansi.write(ansi.paint(
            f"  {terminal.iterations} iterations · {usage.get('prompt', 0):,} tokens in context",
            "dim"))
