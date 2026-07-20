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
import os
import shlex
import uuid
from pathlib import Path

from forge.cell.base import Cell, CellPolicy, CommandResult

WORKDIR = "/workspace"
# The Cell runs as this uid — never root, and never configurable, so a job can
# not talk its way into a privileged container.
CELL_UID = 1000


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
            "--user", f"{CELL_UID}:{CELL_UID}",  # non-root
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
        ]
        if not self.policy.allow_network:
            args += ["--network", "none"]       # default posture: no outbound network (§8)
        if self.workspace_mount is not None:
            self.workspace_mount.mkdir(parents=True, exist_ok=True)
            self._hand_mount_to_cell_user()
            args += ["--mount", f"type=bind,src={self.workspace_mount},dst={WORKDIR}"]
        args += [self.image, "sleep", "infinity"]
        code, _out, err = await self._docker(*args, timeout=60)
        if code != 0:
            raise DockerError(f"could not start Cell container: {err.decode('utf-8', 'replace')}")
        # Ensure the workspace exists and is writable by the non-root user. On a
        # bind mount this can only fix an ephemeral (in-image) workdir: the
        # container drops ALL capabilities, so even uid 0 has no CAP_CHOWN and
        # this call cannot touch host-owned files. _hand_mount_to_cell_user did
        # that part from outside, where the privilege actually exists.
        await self._docker("exec", "--user", "0:0", self.container,
                           "sh", "-c",
                           f"mkdir -p {WORKDIR} && chown {CELL_UID}:{CELL_UID} {WORKDIR} 2>/dev/null || true",
                           timeout=15)
        # Fail loud rather than hand the Warden a Cell whose every write dies
        # with EACCES halfway through a job (§9.5: assume the environment).
        code, _o, _e = await self._docker(
            "exec", self.container, "sh", "-c", f"test -w {WORKDIR}", timeout=15)
        if code != 0:
            await self.close()
            raise DockerError(
                f"Cell workspace {WORKDIR} is not writable by uid {CELL_UID}. "
                f"Give {self.workspace_mount} to that uid, or make it group-writable "
                f"for a group the uid belongs to."
            )
        self._started = True

    def _hand_mount_to_cell_user(self) -> None:
        """Give the bind mount to the Cell's uid, from outside the container.

        This has to happen here because it cannot happen in there: the Cell
        drops every capability, so it holds no CAP_CHOWN even as uid 0 and a
        root-owned bind mount stays unwritable no matter what the container
        does to it. Best-effort by design — when the Forge is unprivileged this
        is a no-op, and the writability probe in start() is what turns a
        still-unusable workspace into a loud error instead of a job that fails
        on its first write.

        The group is deliberately left alone. On the deployed host it is the
        vault's group, and that is what lets the file desktop manage the output
        of a job it did not run.
        """
        chown = getattr(os, "chown", None)       # POSIX only; absent on Windows
        if chown is None or self.workspace_mount is None:
            return
        try:
            st = self.workspace_mount.stat()
            if st.st_uid != CELL_UID:
                chown(self.workspace_mount, CELL_UID, -1)
            # Keep the group's access at least as wide as the owner's, so a
            # shared vault group can still read and clean up what lands here.
            os.chmod(self.workspace_mount, st.st_mode | 0o070)
        except OSError:
            return                               # unprivileged: start() will report it

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
