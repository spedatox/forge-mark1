"""Mark VI peer (§7, §9.2).

Connects to Mark VI's agents WebSocket as the agent it was launched as and speaks
the backend's existing protocol:

    → agent_register (agent_id, capabilities, model_preference)
    → heartbeat (periodic)
    ← task_dispatch {task_id, from, task, cwd}     → run one job → task_result
    ← chat_request  {chat_id, history, cwd, ...}   → run one job → chat_event stream
    ← chat_cancel   {chat_id}                       → abort that run
    ← shutdown                                      → stop, no reconnect

The peer carries no identity of its own — `cfg` is whichever AgentConfig it was
started with, so the same class serves Optimus, Centurion, or a third agent
(§2). Graceful fallback is Mark VI's side of the contract (§9.2): when this peer
is offline, Mark VI answers with its in-process profile.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from forge.agents.config import AgentConfig
from forge.config import ForgeSettings
from forge.gate.protocol import (JobEvent, job_event_to_chat_event,
                                 job_from_chat_request, job_from_task_dispatch)
from forge.extensions import load_extensions
from forge.warden.oracle import Answer, ChannelOracle
from forge.warden.permissions import AllowList
from forge.gate.runner import run_job
from forge.warden.state import StopReason

logger = logging.getLogger("forge.gate.peer")

_BACKOFF_START_S = 1.0
_BACKOFF_MAX_S = 60.0
_HEARTBEAT_S = 30.0
# chat_event types Mark VI's ExternalAgentProxy understands (its _EVENT_MAP).
_CHAT_FORWARD = frozenset({"chunk", "tool", "tool_result", "done", "error"})


class ForgePeer:
    def __init__(self, cfg: AgentConfig, settings: ForgeSettings,
                 registry: "Any") -> None:
        self.cfg = cfg
        self.settings = settings
        self.registry = registry
        # Process-wide extension layer (law 2: assembled once, at the entry
        # point, never via import side effects). Loaded here rather than per job
        # so an MCP server is started once and shared, not respawned per task.
        self.extensions = load_extensions()
        # The operator's standing approvals, shared across every job this peer
        # runs. Loaded once: "don't ask me again" that expired with the job
        # would not mean what anyone reads it as.
        self.allowlist = AllowList.load(settings.allowlist_path)
        # One oracle for the peer — the socket is the channel, so parked asks
        # from any job resolve through the same frame handler.
        self._oracle = ChannelOracle(self._send, timeout_s=settings.ask_timeout_s)
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._chats: dict[str, asyncio.Event] = {}     # chat_id/task_id → abort signal
        self._work: set[asyncio.Task] = set()
        self._shutdown = False
        self._stop = asyncio.Event()

    # ── Connection lifecycle ─────────────────────────────────────────────────
    async def run_forever(self) -> None:
        backoff = _BACKOFF_START_S
        while not self._shutdown and not self._stop.is_set():
            try:
                await self._serve_one()
                backoff = _BACKOFF_START_S
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("peer_connection_lost", extra={"error": f"{type(e).__name__}: {e}"})
            if self._shutdown or self._stop.is_set():
                break
            logger.info("peer_reconnect", extra={"in_s": backoff})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _BACKOFF_MAX_S)

    def request_stop(self) -> None:
        self._stop.set()

    async def _serve_one(self) -> None:
        import websockets
        headers = {"X-API-Key": self.settings.speda_api_key}
        # websockets ≥13 renamed extra_headers → additional_headers.
        try:
            major = int(websockets.__version__.split(".")[0])
        except (AttributeError, ValueError):
            major = 12
        kw = "additional_headers" if major >= 13 else "extra_headers"
        async with websockets.connect(self.settings.speda_ws_url, **{kw: headers}) as ws:
            self._ws = ws
            try:
                await self._register()
                logger.info("peer_registered",
                            extra={"agent": self.cfg.agent_id, "url": self.settings.speda_ws_url})
                hb = asyncio.create_task(self._heartbeat())
                # The receive loop parks on ws.recv() and only returns when the
                # socket closes, so waiting on it alone leaves a stop request
                # unobserved for as long as the connection stays healthy —
                # under systemd that means SIGTERM is ignored until SIGKILL.
                # Race the two instead: whichever lands first ends the session.
                recv = asyncio.create_task(self._receive_loop())
                stop = asyncio.create_task(self._stop.wait())
                try:
                    done, _ = await asyncio.wait(
                        {recv, stop}, return_when=asyncio.FIRST_COMPLETED
                    )
                    # Re-raise a connection failure so run_forever's backoff sees
                    # it; a stop request is not an error and must not reconnect.
                    if recv in done:
                        recv.result()
                finally:
                    hb.cancel()
                    recv.cancel()
                    stop.cancel()
                    await asyncio.gather(hb, recv, stop, return_exceptions=True)
            finally:
                self._ws = None
                # Losing the socket is not a reason to hang, and it is certainly
                # not a reason to proceed: every question still waiting for an
                # answer resolves to denied.
                self._oracle.abandon_all("the operator channel closed")
                for ev in self._chats.values():
                    ev.set()

    async def _send(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("peer socket not connected")
        async with self._send_lock:
            await ws.send(json.dumps(frame))

    async def _register(self) -> None:
        await self._send({
            "type": "agent_register",
            "agent_id": self.cfg.agent_id,
            "agent_name": self.cfg.name,
            "domain": self.cfg.domain,
            "capabilities": list(self.cfg.tool_names),
            "status": "online",
            "model_preference": self.cfg.model_ref,
        })

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_S)
            try:
                await self._send({"type": "heartbeat", "agent_id": self.cfg.agent_id, "payload": {}})
            except Exception:  # noqa: BLE001 — the receive loop owns the disconnect
                return

    async def _receive_loop(self) -> None:
        async for raw in self._ws:
            try:
                frame = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(frame, dict):
                continue
            self._dispatch(frame)
            if self._shutdown:
                return

    def _dispatch(self, frame: dict[str, Any]) -> None:
        ftype = frame.get("type")
        if ftype == "task_dispatch":
            self._spawn(self._handle_task(frame))
        elif ftype == "chat_request":
            self._spawn(self._handle_chat(frame))
        elif ftype == "chat_cancel":
            ev = self._chats.get(str(frame.get("chat_id", "")))
            if ev is not None:
                ev.set()
        elif ftype == "permission_response":
            # Additive frame (law 3). An answer to a question that already timed
            # out is dropped, not an error — a slow operator is not a bug.
            ask_id = str(frame.get("ask_id", ""))
            answer = Answer(approved=bool(frame.get("approved")),
                            remember=bool(frame.get("remember")),
                            note=str(frame.get("note", "")))
            if not self._oracle.resolve(ask_id, answer):
                logger.info("permission_response_unmatched", extra={"ask_id": ask_id})
        elif ftype == "shutdown":
            logger.info("peer_shutdown_requested")
            self._shutdown = True
        # acknowledge / anything else: ignore

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._work.add(task)
        task.add_done_callback(self._work.discard)

    # ── Handlers ─────────────────────────────────────────────────────────────
    async def _handle_task(self, frame: dict[str, Any]) -> None:
        """task_dispatch → run one job, answer with a single task_result."""
        request = job_from_task_dispatch(frame, self.cfg.agent_id)
        signal = asyncio.Event()
        self._chats[request.job_id] = signal

        async def sink(_ev: JobEvent) -> None:      # fire-and-await: no streaming
            return None

        try:
            term = await run_job(request, settings=self.settings, registry=self.registry,
                                 emit=sink, signal=signal,
                                 tool_providers=self.extensions.tool_providers(),
                                 fragments=self.extensions.fragments,
                                 hooks=self.extensions.hooks,
                                 oracle=self._oracle, allowlist=self.allowlist)
            status = "ok" if term.reason is StopReason.COMPLETED else "error"
            result = term.final_text or (term.error or "(no output)")
            await self._send({
                "type": "task_result", "agent_id": self.cfg.agent_id,
                "task_id": request.job_id, "result": result, "status": status,
            })
        finally:
            self._chats.pop(request.job_id, None)

    async def _handle_chat(self, frame: dict[str, Any]) -> None:
        """chat_request → run one job, stream chat_event frames until terminal."""
        request = job_from_chat_request(frame, self.cfg.agent_id)
        chat_id = str(frame.get("chat_id", request.job_id))
        signal = asyncio.Event()
        self._chats[chat_id] = signal
        terminal_seen = False

        async def emit(ev: JobEvent) -> None:
            nonlocal terminal_seen
            if ev.type not in _CHAT_FORWARD:
                return
            if ev.type in ("done", "error"):
                terminal_seen = True
            await self._send({"type": "chat_event", "agent_id": self.cfg.agent_id,
                              "chat_id": chat_id, "event": job_event_to_chat_event(ev)})

        try:
            term = await run_job(request, settings=self.settings, registry=self.registry,
                                 emit=emit, signal=signal,
                                 tool_providers=self.extensions.tool_providers(),
                                 fragments=self.extensions.fragments,
                                 hooks=self.extensions.hooks,
                                 oracle=self._oracle, allowlist=self.allowlist)
            if not terminal_seen:
                # Ensure Mark VI always gets a terminal frame (abort path, etc.).
                final_type = "done" if term.reason is StopReason.COMPLETED else "error"
                data = term.final_text if final_type == "done" else (term.error or "run ended")
                await self._send({"type": "chat_event", "agent_id": self.cfg.agent_id,
                                  "chat_id": chat_id, "event": {"type": final_type, "data": data}})
        finally:
            self._chats.pop(chat_id, None)


def main() -> int:
    """`python -m forge.gate.peer` — connect as the agent named in FORGE_AGENT."""
    import os
    import signal as signalmod
    from forge.agents.registry import AgentRegistry

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = ForgeSettings.from_env()
    if not settings.speda_api_key:
        raise SystemExit("SPEDA_API_KEY is required — the peer authenticates the WS handshake with it.")
    registry = AgentRegistry.load()
    agent_id = os.environ.get("FORGE_AGENT", "optimus")
    cfg = registry.get(agent_id)
    peer = ForgePeer(cfg, settings, registry)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signalmod.SIGINT, signalmod.SIGTERM):
            try:
                loop.add_signal_handler(sig, peer.request_stop)
            except (NotImplementedError, OSError):
                signalmod.signal(sig, lambda *_: peer.request_stop())
        await peer.run_forever()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
