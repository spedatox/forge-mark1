"""Skills — operator-authored procedures, loaded from disk (H10).

A skill is a folder with a `SKILL.md`: frontmatter naming it and saying when it
applies, then a body of instructions. Nothing about it is code, which is the
point — an operator who knows how a deployment must be done should be able to
write that down without writing Python.

**Catalogued, not injected.** Every skill's *description* reaches the model; only
the body of the one it asks for does. Injecting twenty skill bodies into every
job would spend the window on procedures the job will never touch, and the
catalog is what lets the model decide which is relevant. This is the whole reason
skills are a tool rather than a prompt fragment.

**`allowed-tools` is advisory here.** A skill may name the tools it expects, and
that text reaches the model as guidance — but it does not widen the job's
toolset. The profile's allowlist is the security boundary, and a file dropped in
a directory must not be able to grant itself capabilities the operator did not
give the agent. Tightening would be legitimate; widening never is, and the two
are one flag apart, so neither is wired until there is a reason.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("forge.skills")

MAX_SKILL_CHARS = 20_000
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    source: Path
    allowed_tools: tuple[str, ...] = ()

    @property
    def catalog_line(self) -> str:
        return f"- {self.name}: {self.description}"


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split `---` frontmatter from the body.

    A deliberately small subset of YAML: `key: value`, one per line. Skills are
    operator-authored notes, not a configuration language, and pulling in a YAML
    parser to read four keys would be a dependency bought for nothing."""
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip().strip("'\"")
    return meta, match.group(2)


def _split_list(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.replace(",", " ").split() if part.strip())


def load_skill(path: Path) -> Skill | None:
    """Read one `SKILL.md`. Returns None — loudly — if it is unusable.

    A malformed skill is skipped rather than fatal. The operator's other skills,
    and the job itself, should not be held hostage by one file with a typo in
    its frontmatter; the log line is what makes the omission findable."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("skill_unreadable", extra={"path": str(path), "error": repr(e)})
        return None

    meta, body = parse_frontmatter(text)
    name = meta.get("name") or path.parent.name
    description = meta.get("description", "").strip()
    if not description:
        logger.warning("skill_missing_description", extra={"path": str(path)})
        return None
    if not body.strip():
        logger.warning("skill_has_no_instructions", extra={"path": str(path)})
        return None

    if len(body) > MAX_SKILL_CHARS:
        body = body[:MAX_SKILL_CHARS] + "\n\n…[skill truncated]"
    return Skill(name=name, description=description, instructions=body.strip(),
                 source=path, allowed_tools=_split_list(meta.get("allowed-tools", "")))


def load_skills(*roots: Path) -> dict[str, Skill]:
    """Load every `<root>/<skill>/SKILL.md`, earlier roots winning on conflict.

    Earlier wins so a caller can order roots by authority and have the more
    specific source take precedence, rather than the discovery order deciding."""
    found: dict[str, Skill] = {}
    for root in roots:
        if not root or not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            manifest = entry / "SKILL.md"
            if not (entry.is_dir() and manifest.is_file()):
                continue
            skill = load_skill(manifest)
            if skill is None:
                continue
            if skill.name in found:
                logger.info("skill_shadowed", extra={"name": skill.name,
                                                     "ignored": str(manifest)})
                continue
            found[skill.name] = skill
    return found


def catalog_fragment_text(skills: dict[str, Skill]) -> str:
    """The lines that tell the model which procedures exist."""
    lines = "\n".join(skills[name].catalog_line for name in sorted(skills))
    return (
        "Procedures are available for the following. When one applies, load it "
        "with the `skill` tool BEFORE starting the work — it carries specifics "
        "that are not in this prompt, and guessing at a documented procedure is "
        "how a job gets it subtly wrong.\n\n"
        f"{lines}"
    )
