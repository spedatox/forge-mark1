"""Seam 7 — system prompts are composed, not concatenated.

`AgentConfig.system_prompt` is one opaque string. The moment two things want to
add to it — repo conventions, a shared git discipline, later a skill — the
assembly point turns into string-concatenation soup, and nothing downstream can
answer "where did this instruction come from".

Fragments carry their source, order is fixed policy rather than call-site
accident, and the result is delimited so the model can tell a repo's rules from
its own identity. A Mark III skill is, at its core, a fragment plus a tool
allowlist — which is Seam 7 plus Seam 1, both of which then already exist.

Composition happens once per job, before the first model call, so fragments cost
nothing per iteration and sit inside whatever prefix a provider caches.
"""
from __future__ import annotations

from dataclasses import dataclass

# Fixed order, most-general first. Identity before discipline before local rules:
# a repo's conventions should be able to refine the agent's standing habits, and
# the last word on a specific point should belong to the most specific source.
ORDER = ("profile", "shared", "repo", "skill")


@dataclass(frozen=True)
class PromptFragment:
    """One labelled contribution to a system prompt."""
    source: str      # "profile" | "shared:git" | "repo:CLAUDE.md" | "skill:<name>"
    text: str

    @property
    def kind(self) -> str:
        return self.source.split(":", 1)[0]


def _rank(fragment: PromptFragment) -> int:
    try:
        return ORDER.index(fragment.kind)
    except ValueError:
        return len(ORDER)      # unknown kinds sort last, deterministically


def compose_system_prompt(fragments: list[PromptFragment]) -> str:
    """Render fragments into one system prompt.

    A fragment's origin is stated in the output. That is not decoration: an
    agent that has been handed a repo's conventions and its own instructions
    needs to know which is which to resolve a conflict between them, and an
    operator reading a transcript needs to know why the agent believed
    something. The profile's own text is unlabelled, because it is the agent
    speaking as itself."""
    ordered = sorted([f for f in fragments if f.text.strip()],
                     key=lambda f: (_rank(f), fragments.index(f)))
    parts: list[str] = []
    for fragment in ordered:
        if fragment.source == "profile":
            parts.append(fragment.text.strip())
        else:
            label = fragment.source.split(":", 1)[-1].upper()
            parts.append(f"═══ {label} ═══\n{fragment.text.strip()}")
    return "\n\n".join(parts)
