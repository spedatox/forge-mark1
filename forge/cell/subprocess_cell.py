"""SubprocessCell — reduced-isolation Cell backend for Docker-less dev / CI.

Same four-method contract as DockerCell, but the boundary is a per-agent
workspace directory plus wall-clock and output caps, not a kernel namespace.
It is honest about what it is: a workspace jail, NOT a security boundary against
hostile code. Use DockerCell in production (§8). The factory picks this only
when Docker is unavailable or explicitly requested.

Every command runs with cwd pinned to the workspace; read/write refuse paths
that escape it. Network cannot be portably severed from a plain subprocess, so
when the policy forbids network this backend sets proxy-blackhole env vars as a
best-effort deterrent and the README documents the gap.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from forge.cell.base import Cell, CellPolicy, CommandResult


class SubprocessCell(Cell):
    def __init__(self, workspace: Path, policy: CellPolicy) -> None:
        self.workspace = Path(workspace).resolve()
        self.policy = policy

    async def start(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _base_env(self, extra: dict[str, str] | None) -> dict[str, str]:
        env = dict(os.environ)
        # Never leak the operator's model/provider keys into executed code.
        for secret in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                       "SPEDA_API_KEY", "ZAI_API_KEY", "DEEPSEEK_API_KEY"):
            env.pop(secret, None)
        if not self.policy.allow_network:
            # Best-effort only (subprocess isolation cannot truly cut the network).
            env.update({"http_proxy": "http://127.0.0.1:9",
                        "https_proxy": "http://127.0.0.1:9",
                        "HTTP_PROXY": "http://127.0.0.1:9",
                        "HTTPS_PROXY": "http://127.0.0.1:9",
                        "no_proxy": ""})
        if extra:
            env.update(extra)
        return env

    def _resolve(self, path: str) -> Path:
        target = (self.workspace / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
        if os.path.commonpath([str(target), str(self.workspace)]) != str(self.workspace):
            raise PermissionError(f"path escapes the Cell workspace: {path!r}")
        return target

    async def run(self, command: str, timeout: int | None = None,
                  env: dict[str, str] | None = None) -> CommandResult:
        t = self._clamp_timeout(timeout)
        self.workspace.mkdir(parents=True, exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.workspace),
                env=self._base_env(env),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return CommandResult("", f"failed to launch command: {e}", 1, False)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=t)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return CommandResult("", f"command timed out after {t}s", 124, True)
        return CommandResult(
            self._cap(out.decode("utf-8", "replace")),
            self._cap(err.decode("utf-8", "replace")),
            proc.returncode if proc.returncode is not None else 1,
            False,
        )

    async def write(self, path: str, content: str) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def read(self, path: str) -> str:
        return self._resolve(path).read_text(encoding="utf-8")

    async def reset(self) -> None:
        if self.workspace.exists():
            shutil.rmtree(self.workspace, ignore_errors=True)
        self.workspace.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        # The workspace persists on disk for inspection; nothing to tear down.
        return None
