"""Permission and safety — deliberately minimal (§6).

One short precedence chain, ending in a hard, non-bypassable safety gate for
irreversible / high-blast-radius operations:

    1. session allow-list        operator's known-safe repeated actions → allow
    2. tool.check_permissions    the tool's own opinion (deny / allow / defer)
    3. SAFETY GATE (bypass-immune)  version-control internals, credentials, shell
                                    config, destructive-marked ops → DENY, even if
                                    steps 1–2 said allow
    4. mode                       plan → deny mutations; act → allow

One active mode (`act`) plus an optional read-only `plan` mode (§6). No LLM risk
classifier, no multi-source rule layering, no denial-tracking — the operator is
the risk assessor, and there is no second party to govern.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ── The gate's target sets. These are about irreversibility and reach, not trust
# (study §5) — they fire even for a fully trusted operator. ────────────────────

_SENSITIVE_PATH_PATTERNS = [
    r"(^|/)\.git/",             # version-control internals
    r"(^|/)\.ssh(/|$)", r"id_rsa", r"id_ed25519",
    r"(^|/)\.aws(/|$)", r"(^|/)\.gcp(/|$)", r"(^|/)\.kube(/|$)",
    r"(^|/)\.env($|\.)", r"credentials", r"\.pem$", r"\.key$",
    r"(^|/)\.npmrc$", r"(^|/)\.pypirc$", r"(^|/)\.netrc$",
    r"(^|/)\.docker/config",
    r"(^|/)\.bashrc$", r"(^|/)\.bash_profile$", r"(^|/)\.profile$",
    r"(^|/)\.zshrc$", r"(^|/)\.zprofile$",
    r"Microsoft\.PowerShell_profile\.ps1",
]

_DESTRUCTIVE_CMD_PATTERNS = [
    r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\b",   # rm -rf / -fr
    r"\bgit\s+push\b.*(--force|\s-f\b)",               # force push
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-[a-z]*f",
    r"\bchmod\s+-R\s+777\b",
    r"\bmkfs\b", r"\bdd\s+if=", r">\s*/dev/sd",
    r":\(\)\s*\{", r"\bshutdown\b", r"\breboot\b",
    r"\bsudo\b",
    r"\bcurl\b[^|]*\|\s*(sudo\s+)?(sh|bash)\b",         # curl … | sh
    r"\bwget\b[^|]*\|\s*(sudo\s+)?(sh|bash)\b",
]

_SENSITIVE_RE = [re.compile(p, re.IGNORECASE) for p in _SENSITIVE_PATH_PATTERNS]
_DESTRUCTIVE_RE = [re.compile(p, re.IGNORECASE) for p in _DESTRUCTIVE_CMD_PATTERNS]


class Mode(str, Enum):
    ACT = "act"      # the one working mode: act, with the safety gate always on
    PLAN = "plan"    # read-only review: mutations are denied outright


@dataclass
class Decision:
    behavior: str    # "allow" | "deny"
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.behavior == "allow"


ALLOW = Decision("allow")


def _flag(tool: Any, name: str, args: Any) -> bool:
    """Ask a tool one of its per-input safety questions, failing closed.

    Closed means different things for different questions, and the caller's
    framing decides: `is_destructive` failing closed is True (gate it), while
    `is_read_only` failing closed is False (treat it as a mutation). Both come
    out of the same rule — assume the answer that restricts."""
    check = getattr(tool, name, None)
    if check is None:
        return False
    try:
        return bool(check(args))
    except Exception:  # noqa: BLE001 — an undecidable flag is a gated one
        return name == "is_destructive"


@dataclass
class AllowList:
    """A tiny session allow-store so the operator's known-safe, repeated actions
    stop re-prompting (§6). Entries are `tool_name` (whole tool) or
    `tool_name:glob` matched against the action's key (command or path).
    Deliberately one source — no policy/project/enterprise layering."""
    entries: set[str] = field(default_factory=set)

    def add(self, entry: str) -> None:
        self.entries.add(entry)

    def allows(self, tool_name: str, key: str | None) -> bool:
        if tool_name in self.entries:
            return True
        if key is None:
            return False
        for e in self.entries:
            if ":" not in e:
                continue
            name, pat = e.split(":", 1)
            if name == tool_name and fnmatch.fnmatch(key, pat):
                return True
        return False


def gate_reason(tool: Any, args: Any, args_dict: dict[str, Any]) -> str | None:
    """Return a reason string if (tool, args) is an irreversible / high-blast-
    radius operation the gate must stop; else None. Tool-agnostic: it asks the
    tool whether this call is destructive and inspects any `path` / `command`
    arguments, so it needs no per-tool wiring."""
    if _flag(tool, "is_destructive", args):
        return "this call is destructive (irreversible operation)"
    path = args_dict.get("path")
    if isinstance(path, str):
        for rx in _SENSITIVE_RE:
            if rx.search(path.replace("\\", "/")):
                return f"path {path!r} is a protected location (VCS/credentials/shell config)"
    command = args_dict.get("command")
    if isinstance(command, str):
        for rx in _DESTRUCTIVE_RE:
            if rx.search(command):
                return f"command matches a high-blast-radius pattern ({rx.pattern})"
    return None


class PermissionEngine:
    def __init__(self, mode: Mode = Mode.ACT, allowlist: AllowList | None = None) -> None:
        self.mode = mode
        self.allowlist = allowlist or AllowList()

    @staticmethod
    def _action_key(args_dict: dict[str, Any]) -> str | None:
        """The value the allow-list matches against for this action."""
        for field_name in ("command", "path"):
            v = args_dict.get(field_name)
            if isinstance(v, str):
                return v
        return None

    def resolve(self, tool: Any, args: Any, ctx: Any) -> Decision:
        """Decide one call. `args` is the tool's validated argument model — the
        per-input safety flags cannot be asked without it."""
        args_dict = args.model_dump() if hasattr(args, "model_dump") else dict(args)
        key = self._action_key(args_dict)

        # (3) The safety gate is computed first because it is BYPASS-IMMUNE: an
        # allow-list hit or act mode can never override it.
        gate = gate_reason(tool, args, args_dict)

        # (1) Session allow-list — but never for a gated operation.
        if gate is None and self.allowlist.allows(tool.name, key):
            return ALLOW

        # (2) Tool's own opinion.
        own = tool.check_permissions(args_dict, ctx) if hasattr(tool, "check_permissions") else None
        if own is not None and own.behavior == "deny":
            return own

        # (3) Enforce the gate.
        if gate is not None:
            return Decision(
                "deny",
                f"blocked by the safety gate: {gate}. This operation is "
                f"irreversible or high-blast-radius; the operator must allow-list "
                f"it explicitly (add '{tool.name}:{key}' to the session allow-list) "
                f"if it is intended.",
            )

        # (4) Mode.
        if self.mode is Mode.PLAN and not _flag(tool, "is_read_only", args):
            return Decision("deny", "plan mode is active: mutating tools are disabled for review.")

        if own is not None and own.behavior == "allow":
            return own
        return ALLOW
