"""The local interactive surface (TUI).

Rendering is tested by capturing what reaches stdout, and the session by driving
commands against a real Session. The permission oracle gets the most attention:
it is the third implementation of Seam 2, and the one where a wrong answer is a
security failure rather than a cosmetic one.
"""
import asyncio
import io
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from forge.agents.config import AgentConfig, CellSpec
from forge.tui import ansi
from forge.tui.commands import REGISTRY, resolve
from forge.tui.render import StreamRenderer, _summarize_args, banner
from forge.tui.session import Session, TerminalOracle
from forge.warden.ledger import TokenLedger
from forge.warden.permissions import AllowList
from forge.warden.tool import Tool, ToolContext, ToolResult


class NoteArgs(BaseModel):
    path: str = ""


class Note(Tool):
    name = "note"
    description = "Write a note. Second sentence that should not appear in /tools."
    Args = NoteArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True

    async def call(self, args, ctx):
        return ToolResult("ok")


def _session(**over) -> Session:
    cfg = AgentConfig(agent_id="optimus", name="Optimus", domain="engineering",
                      model_ref="anthropic:test-model", tool_names=("note",),
                      system_prompt="You are Optimus.", cell=CellSpec())
    base = dict(cfg=cfg, model_ref="anthropic:test-model", workspace=Path("/repo"),
                tools={"note": Note()}, ledger=TokenLedger(), allowlist=AllowList())
    base.update(over)
    return Session(**base)


def _capture(fn) -> str:
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        fn()
    return buf.getvalue()


# ── Encoding: the thing that killed the first run ───────────────────────────
def test_glyphs_fall_back_when_the_terminal_cannot_encode_them():
    """A Windows console defaults to cp1252 and cannot encode a single one of
    these. An UnicodeEncodeError while drawing the banner would end the session
    before the operator typed anything."""
    with patch.object(ansi, "_UNICODE", False):
        out = ansi.glyphs("▲ FORGE ⏺ tool ↳ result ✗ error")
    assert "▲" not in out and "⏺" not in out
    assert "FORGE" in out and "error" in out


def test_glyphs_pass_through_when_the_terminal_can():
    with patch.object(ansi, "_UNICODE", True):
        assert ansi.glyphs("▲ FORGE") == "▲ FORGE"


def test_write_never_raises_on_an_impossible_encoding():
    """Output is the one thing that must not be able to end the session."""
    class Cp1252(io.StringIO):
        encoding = "cp1252"

        def write(self, s):
            s.encode("cp1252")          # raises exactly as the console would
            return super().write(s)

    buf = Cp1252()
    with patch("sys.stdout", buf), patch.object(ansi, "_UNICODE", True):
        ansi.write("▲ FORGE")           # unicode ON but stream cannot take it
    assert "FORGE" in buf.getvalue()


def test_colour_is_skipped_when_the_stream_is_not_a_terminal():
    with patch.object(ansi, "_ENABLED", False):
        assert ansi.paint("text", "red") == "text"


def test_truncate_flattens_newlines_so_a_row_stays_a_row():
    assert ansi.truncate("a\nb", 40) == "a ⏎ b"
    assert ansi.truncate("x" * 100, 10).endswith("…")


# ── Rendering ────────────────────────────────────────────────────────────────
def _render(events, verbose=False) -> str:
    r = StreamRenderer(verbose=verbose)

    def go():
        for e in events:
            asyncio.run(r(e))
    return _capture(go)


def test_model_prose_and_harness_actions_are_visually_distinct():
    """The one distinction a transcript must never blur."""
    out = _render([
        {"type": "chunk", "data": "Looking at it."},
        {"type": "tool", "data": {"id": "1", "name": "grep", "input": {"pattern": "TODO"}}},
        {"type": "tool_result", "data": {"tool_use_id": "1", "content": "3 files", "is_error": False}},
    ])
    assert "Looking at it." in out
    assert "grep" in out and "TODO" in out
    # Harness lines are indented; the model's prose is at the margin.
    assert any(line.startswith("  ") and "grep" in line for line in out.splitlines())


def test_a_failed_tool_is_marked_as_such():
    out = _render([{"type": "tool_result",
                    "data": {"tool_use_id": "1", "content": "boom", "is_error": True}}])
    assert "boom" in out


def test_results_are_one_line_unless_verbose():
    long = "\n".join(f"line {i}" for i in range(50))
    quiet = _render([{"type": "tool_result", "data": {"tool_use_id": "1", "content": long}}])
    loud = _render([{"type": "tool_result", "data": {"tool_use_id": "1", "content": long}}],
                   verbose=True)
    assert len(quiet.splitlines()) == 1
    assert len(loud.splitlines()) > 10


def test_compaction_is_announced_to_the_operator():
    out = _render([{"type": "compact", "data": {"stage": "elide", "freed_chars": 12000}}])
    assert "12,000" in out and "context" in out


def test_the_argument_summary_names_the_target():
    """A tool row is only useful if it says what the call will act on."""
    assert _summarize_args({"command": "pytest -q"}) == "pytest -q"
    assert _summarize_args({"path": "src/app.py"}) == "src/app.py"
    assert _summarize_args({}) == ""


def test_unknown_events_are_ignored_rather_than_crashing():
    """The vocabulary is open and grows; an old TUI must survive a new event."""
    assert _render([{"type": "some_future_event", "data": {"x": 1}}]) == ""


def test_the_banner_states_where_the_agent_is_working():
    out = banner("Optimus", "anthropic:x", "/repo", 9)
    assert "Optimus" in out and "/repo" in out and "9 tools" in out


# ── The permission oracle (Seam 2, third implementation) ────────────────────
def _ask(answer: str):
    oracle = TerminalOracle()
    with patch("builtins.input", lambda *a: answer), patch("sys.stdout", io.StringIO()):
        return asyncio.run(oracle.ask("run_command", "git push --force", "gated"))


def test_yes_approves_only_this_once():
    a = _ask("y")
    assert a.approved and not a.remember


def test_always_approves_and_remembers():
    a = _ask("a")
    assert a.approved and a.remember


@pytest.mark.parametrize("reply", ["n", "no", "", "anything else", "Y E S"])
def test_everything_that_is_not_yes_is_a_refusal(reply):
    """Fail closed: only an explicit yes approves an irreversible operation."""
    assert not _ask(reply).approved


def test_interrupting_the_prompt_is_a_refusal():
    """Ctrl-C at the question is a clear way to say no, not a crash."""
    oracle = TerminalOracle()

    def boom(*a):
        raise KeyboardInterrupt

    with patch("builtins.input", boom), patch("sys.stdout", io.StringIO()):
        answer = asyncio.run(oracle.ask("run_command", "rm -rf /", "gated"))
    assert not answer.approved


def test_the_command_is_shown_verbatim_and_unwrapped():
    """Approving a force-push means seeing which branch."""
    oracle = TerminalOracle()
    buf = io.StringIO()
    with patch("builtins.input", lambda *a: "n"), patch("sys.stdout", buf):
        asyncio.run(oracle.ask("run_command", "git push --force origin production", "gated"))
    assert "git push --force origin production" in buf.getvalue()


# ── Commands ─────────────────────────────────────────────────────────────────
def _run(line: str, session: Session):
    cmd, args = resolve(line)
    assert cmd is not None, f"no command for {line!r}"
    return asyncio.run(cmd.run(args, session))


def test_unknown_commands_resolve_to_nothing():
    assert resolve("/nope")[0] is None
    assert resolve("/")[0] is None


def test_help_lists_every_command_once():
    out = _run("/help", _session()).text
    assert "/help" in out and "/cost" in out and "/compact" in out
    assert out.count("/cost") == 1, "aliases must not duplicate a row"


def test_aliases_reach_the_same_command():
    assert resolve("/q")[0] is resolve("/exit")[0]
    assert resolve("/?")[0] is resolve("/help")[0]


def test_exit_asks_the_session_to_end():
    assert _run("/exit", _session()).quit is True


def test_clear_asks_the_session_to_forget():
    assert _run("/clear", _session()).clear is True


def test_context_shows_the_window_and_the_threshold():
    session = _session()
    session.ledger.prompt_tokens = 50_000
    out = _run("/context", session).text
    assert "50,000" in out and "compaction at" in out


def test_context_warns_when_the_next_turn_will_compact():
    session = _session()
    session.ledger.prompt_tokens = session.ledger.compact_at + 1
    assert "will compact" in _run("/context", session).text


def test_cost_says_when_the_figures_are_guesses():
    """Never present an estimate as a measurement."""
    session = _session()
    session.ledger.estimate("sys", [{"role": "user", "content": "hi"}])
    assert "ESTIMATE" in _run("/cost", session).text


def test_cost_is_honest_about_an_empty_session():
    assert "No model turns" in _run("/cost", _session()).text


def test_tools_lists_names_and_safety():
    out = _run("/tools", _session()).text
    assert "note" in out and "read-only" in out and "parallel" in out
    assert "should not appear" not in out, "only the first sentence belongs in a list"


def test_approved_reports_an_empty_allowlist_plainly():
    assert "Nothing approved" in _run("/approved", _session()).text


def test_approved_lists_what_is_on_file():
    session = _session(allowlist=AllowList({"run_command:git push --force"}))
    assert "git push --force" in _run("/approved", session).text


def test_compact_declines_when_there_is_nothing_to_do():
    result = _run("/compact", _session())
    assert not result.compact and "Nothing" in result.text


def test_compact_asks_the_session_when_there_is():
    session = _session()
    session.messages = [{"role": "user", "content": "hi"}]
    assert _run("/compact", session).compact is True


def test_agent_and_model_report_the_profile():
    session = _session()
    assert "optimus" in _run("/agent", session).text
    assert "anthropic:test-model" in _run("/model", session).text


# ── Session state ────────────────────────────────────────────────────────────
def test_clearing_frees_context_without_un_spending_money():
    """`/clear` frees the window; it does not make the session cheaper, and a
    cost display that reset would under-report what was actually spent."""
    session = _session()
    session.messages = [{"role": "user", "content": "hi"}]
    session.ledger.input_tokens = 5_000
    session.ledger.turns = 3
    session.ledger.prompt_tokens = 900

    session.reset()

    assert session.messages == []
    assert session.ledger.prompt_tokens == 0
    assert session.ledger.input_tokens == 5_000 and session.ledger.turns == 3


# ── Error presentation (found by the first live run) ────────────────────────
def test_a_billing_error_says_what_to_do_about_it():
    """A provider error arrives as a JSON blob inside an SDK exception name.
    That is the right thing to log and the wrong thing to show someone who just
    wants to know why their session stopped."""
    from forge.tui.render import humanize_error

    raw = ("Error code: 400 - {'type': 'error', 'error': {'type': "
           "'invalid_request_error', 'message': 'Your credit balance is too low "
           "to access the Anthropic API.'}}")
    out = humanize_error(raw)
    assert "out of credit" in out
    assert "Plans & Billing" in out
    assert "400" in out, "the raw error is kept — the friendly line is a guess"


@pytest.mark.parametrize("raw,expect", [
    ("authentication_error: invalid x-api-key", "rejected"),
    ("rate_limit_error", "Rate limited"),
    ("overloaded_error", "overloaded"),
    ("prompt is too long: 210000 tokens", "/clear"),
])
def test_known_provider_errors_are_translated(raw, expect):
    from forge.tui.render import humanize_error
    assert expect in humanize_error(raw)


def test_an_unrecognized_error_is_passed_through_bounded():
    from forge.tui.render import humanize_error
    assert humanize_error("something new") == "something new"
    assert len(humanize_error("x" * 900)) < 400


def test_the_turn_summary_does_not_repeat_an_error_the_stream_showed():
    """One failure must not look like two."""
    r = StreamRenderer()
    assert r.saw_error is False
    asyncio.run(r({"type": "error", "data": "boom"}))
    assert r.saw_error is True
