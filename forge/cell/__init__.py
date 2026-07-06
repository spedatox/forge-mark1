"""The Cell — one isolated execution sandbox per agent (§8, §9.1).

    Cell.run(command, timeout, env) -> CommandResult{stdout, stderr, exit_code}
    Cell.write(path, content)
    Cell.read(path)
    Cell.reset()

Default posture (§8): no outbound network unless the job requests it; CPU / memory
/ wall-clock capped. The Warden reasons; the Cell only executes (§9.3). A Cell is
never shared between agents (§9.1) — the Gate builds a fresh one per job.
"""
from forge.cell.base import Cell, CommandResult, CellPolicy
from forge.cell.factory import build_cell

__all__ = ["Cell", "CommandResult", "CellPolicy", "build_cell"]
