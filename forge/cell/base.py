"""Cell contract — the exact four-method surface from §8, plus the policy that
governs every backend that implements it."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    """The sole return shape of Cell.run (§8)."""
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False


@dataclass(frozen=True)
class CellPolicy:
    """Resource envelope for a Cell. Defaults are the safe posture (§8):
    no network, bounded CPU / memory / time, output capped."""
    allow_network: bool = False        # outbound network only if the job asks (§8, §9)
    cpus: float = 1.0                   # CPU cores (DockerCell --cpus)
    memory_mb: int = 1024              # memory ceiling (DockerCell --memory)
    pids_limit: int = 256              # fork-bomb guard (DockerCell --pids-limit)
    default_timeout_s: int = 60        # per-command wall clock when a call omits one
    max_timeout_s: int = 600           # hard ceiling a per-command timeout cannot exceed
    max_output_bytes: int = 100_000    # cap returned stdout+stderr so a runaway can't flood


class Cell(abc.ABC):
    """Abstract isolated sandbox. One instance == one agent's world.

    Implementations must guarantee that `run`, `read`, and `write` cannot reach
    outside the Cell's workspace, and that `run` always returns a CommandResult
    (never raises for a non-zero exit or a timeout — those are data, not
    exceptions, mirroring the errors-as-results discipline of the loop)."""

    policy: CellPolicy

    @property
    def host_path(self) -> "Path | None":
        """The workspace as the harness can see it, or None when it cannot.

        Search and navigation run Warden-side over this path — the same
        separation the Graphify sidecar already uses, and for the same reason:
        the Cell's isolation posture governs *generated code*, not the harness's
        own instruments. A backend with no host-visible workspace (an ephemeral
        container, a remote VM) returns None, and those tools report themselves
        unavailable rather than guessing. Concrete, not abstract: None is a
        correct answer, so a new backend is not required to have one."""
        return None

    @abc.abstractmethod
    async def start(self) -> None:
        """Provision the sandbox (create container / workspace). Idempotent."""

    @abc.abstractmethod
    async def run(self, command: str, timeout: int | None = None,
                  env: dict[str, str] | None = None) -> CommandResult:
        """Execute a shell command inside the Cell and return its result."""

    @abc.abstractmethod
    async def write(self, path: str, content: str) -> None:
        """Write text to `path`, interpreted relative to the Cell workspace."""

    @abc.abstractmethod
    async def read(self, path: str) -> str:
        """Read text from `path`, interpreted relative to the Cell workspace."""

    @abc.abstractmethod
    async def reset(self) -> None:
        """Discard all Cell state and return to a clean workspace."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Tear the sandbox down. Called when the job ends."""

    def _clamp_timeout(self, timeout: int | None) -> int:
        t = self.policy.default_timeout_s if timeout is None else int(timeout)
        return max(1, min(t, self.policy.max_timeout_s))

    def _cap(self, text: str) -> str:
        limit = self.policy.max_output_bytes
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n…[truncated {len(text) - limit} bytes]"
