"""DockerCell — the production Cell backend (§8).

One throwaway container per agent, matching the posture of Mark VI's own
`packages/sandbox`: isolated, resource-capped, non-root, no host mounts beyond
the workspace volume, `--network none` unless the job requests network. The
container is long-lived for the job (started once, `sleep infinity`); commands
run via `docker exec`, so filesystem and installed-package state persist across
calls the way a real machine would. `reset()` destroys and recreates it.

Rationale for Docker over a microVM/gVisor (documented in the README): it is the
same technology Mark VI already sandboxes with, it is a single well-understood
dependency, and kernel namespaces + cgroups give a real isolation boundary with
resource caps. A microVM (Firecracker) would be stronger but adds a KVM/Linux
requirement the single-operator Forge does not need for its threat model.
"""
from __future__ import annotations

import asyncio
import base64
import shlex
import uuid
from pathlib import Path

from forge.cell.base import Cell, CellPolicy, CommandResult

WORKDIR = "/workspace"


class DockerError(RuntimeError):
    pass


class DockerCell(Cell):
    def __init__(self, name_hint: str, image: str, policy: CellPolicy,
                 workspace_mount: "Path | None" = None) -> None:
        self.image = image
        self.policy = policy
        # Optional host directory bind-mounted to /workspace — the only host
        # mount, matching Mark VI's packages/sandbox posture. When None, the
        # workspace is ephemeral inside the container.
        self.workspace_mount = Path(workspace_mount).resolve() if workspace_mount else None
        # Unique per instance so two agents can never collide on one container (§9.1).
        self.container = f"forge-cell-{name_hint}-{uuid.uuid4().hex[:8]}"
        self._started = False

    async def _docker(self, *args: str, timeout: int | None = None,
                      stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, b"", b"docker call timed out"
        return proc.returncode if proc.returncode is not None else 1, out, err

    async def start(self) -> None:
        if self._started:
            return
        args = [
            "run", "-d", "--name", self.container,
            "--workdir", WORKDIR,
            "--memory", f"{self.policy.memory_mb}m",
            "--cpus", str(self.policy.cpus),
            "--pids-limit", str(self.policy.pids_limit),
            "--user", "1000:1000",              # non-root
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
        ]
        if not self.policy.allow_network:
            args += ["--network", "none"]       # default posture: no outbound network (§8)
        if self.workspace_mount is not None:
            self.workspace_mount.mkdir(parents=True, exist_ok=True)
            args += ["--mount", f"type=bind,src={self.workspace_mount},dst={WORKDIR}"]
        args += [self.image, "sleep", "infinity"]
        code, _out, err = await self._docker(*args, timeout=60)
        if code != 0:
            raise DockerError(f"could not start Cell container: {err.decode('utf-8', 'replace')}")
        # Ensure the workspace exists and is writable by the non-root user.
        await self._docker("exec", "--user", "0:0", self.container,
                           "sh", "-c", f"mkdir -p {WORKDIR} && chown 1000:1000 {WORKDIR}", timeout=15)
        self._started = True

    async def run(self, command: str, timeout: int | None = None,
                  env: dict[str, str] | None = None) -> CommandResult:
        if not self._started:
            await self.start()
        t = self._clamp_timeout(timeout)
        exec_args = ["exec"]
        for k, v in (env or {}).items():
            exec_args += ["--env", f"{k}={v}"]
        exec_args += [self.container, "sh", "-c", command]
        # `docker exec` has no built-in timeout; wrap the whole exec in wait_for.
        code, out, err = await self._docker(*exec_args, timeout=t)
        timed_out = code == 124
        return CommandResult(
            self._cap(out.decode("utf-8", "replace")),
            self._cap(err.decode("utf-8", "replace")),
            code,
            timed_out,
        )

    def _guard(self, path: str) -> str:
        # Resolve inside WORKDIR; refuse traversal outside it.
        p = path if path.startswith("/") else f"{WORKDIR}/{path}"
        norm = str(Path(p).as_posix())
        if not (norm == WORKDIR or norm.startswith(WORKDIR + "/")):
            raise PermissionError(f"path escapes the Cell workspace: {path!r}")
        return norm

    async def write(self, path: str, content: str) -> None:
        target = self._guard(path)
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        parent = str(Path(target).parent.as_posix())
        cmd = f"mkdir -p {shlex.quote(parent)} && echo {b64} | base64 -d > {shlex.quote(target)}"
        code, _out, err = await self._docker("exec", self.container, "sh", "-c", cmd, timeout=30)
        if code != 0:
            raise DockerError(f"Cell write failed: {err.decode('utf-8', 'replace')}")

    async def read(self, path: str) -> str:
        target = self._guard(path)
        code, out, err = await self._docker("exec", self.container, "cat", target, timeout=30)
        if code != 0:
            raise FileNotFoundError(err.decode("utf-8", "replace") or f"cannot read {path!r}")
        return out.decode("utf-8", "replace")

    async def reset(self) -> None:
        await self.close()
        self._started = False
        await self.start()

    async def close(self) -> None:
        await self._docker("rm", "-f", self.container, timeout=30)
        self._started = False

    @staticmethod
    async def available() -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "info",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            return await asyncio.wait_for(proc.wait(), timeout=8) == 0
        except (OSError, asyncio.TimeoutError):
            return False
