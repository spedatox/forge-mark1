"""The `skill` tool and the provider that supplies it (Seams 1 and 7).

A skill is a prompt fragment plus a tool — which is exactly the two seams H9
landed, so this is assembly-time code and touches no core module. That was the
claim MARK2_SEAMS made about what a skill would cost once the seams existed;
this is the claim being cashed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from forge.agents.prompt import PromptFragment
from forge.skills.loader import Skill, catalog_fragment_text
from forge.warden.tool import Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from pathlib import Path


class SkillArgs(BaseModel):
    name: str = Field(description="Name of the procedure to load, from the catalog.")


class SkillTool(Tool):
    name = "skill"
    description = (
        "Load a documented procedure by name. The system prompt lists which ones "
        "exist and when each applies; this returns the full instructions for one. "
        "Load the procedure before starting work it covers rather than afterwards — "
        "it carries specifics that are not in the prompt. Read-only and safe to run "
        "in parallel."
    )
    Args = SkillArgs
    READ_ONLY = True
    CONCURRENCY_SAFE = True

    def __init__(self, skills: dict[str, Skill]) -> None:
        self._skills = skills

    async def call(self, args: SkillArgs, ctx: ToolContext) -> ToolResult:
        skill = self._skills.get(args.name)
        if skill is None:
            known = ", ".join(sorted(self._skills)) or "(none)"
            return ToolResult(f"No skill named {args.name!r}. Available: {known}.",
                              is_error=True)
        header = f"═══ SKILL: {skill.name} ═══\n{skill.description}\n"
        if skill.allowed_tools:
            # Guidance, not a grant: the profile's allowlist decides what exists.
            header += (f"\nThis procedure expects to use: {', '.join(skill.allowed_tools)}. "
                       f"If one of those is not available to you, say so rather than "
                       f"improvising around it.\n")
        return ToolResult(f"{header}\n{skill.instructions}")


class SkillProvider:
    """Contributes the `skill` tool when any skill was found."""

    name = "skills"

    def __init__(self, skills: dict[str, Skill]) -> None:
        self.skills = skills

    @classmethod
    def from_dirs(cls, *roots: "Path") -> "SkillProvider":
        from forge.skills.loader import load_skills
        return cls(load_skills(*roots))

    async def provide(self, cfg: Any, request: Any) -> dict[str, Tool]:
        # Absent rather than empty when there is nothing to offer: a tool whose
        # every call can only fail is worse than no tool, because the model has
        # to spend a turn discovering that.
        if not self.skills:
            return {}
        return {SkillTool.name: SkillTool(self.skills)}

    async def close(self) -> None:
        return None

    def fragment(self) -> PromptFragment | None:
        """The catalog, for Seam 7. None when there is nothing to catalogue."""
        if not self.skills:
            return None
        return PromptFragment("skill:catalog", catalog_fragment_text(self.skills))
