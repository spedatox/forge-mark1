"""The Forge's local interactive surface.

`demo`, `serve` and `connect` run jobs somebody else asked for. This is the one
entry point where a person is present for the whole run — which makes it where
the permission ask, compaction and the ledger can actually be watched rather
than inferred from a log.
"""
from forge.tui.repl import run_repl

__all__ = ["run_repl"]
