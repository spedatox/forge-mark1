"""Forge CLI.

    python -m forge demo                 run the offline end-to-end demo (no key)
    python -m forge serve [--host --port]  standalone native-contract job server
    python -m forge connect [--agent ID]   connect to Mark VI as a WebSocket peer
    python -m forge agents                 list configured agents

The `connect` path is how Mark VI drives the Forge in production; `serve` and
`demo` run it standalone (§10). There is one mode per invocation — no multi-
entrypoint dispatch inside the process (§3 rejected list).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forge", description="The Forge — SPEDA Mark VI execution peer")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo", help="run the offline end-to-end demo")

    p_serve = sub.add_parser("serve", help="standalone native-contract WS job server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8770)

    p_conn = sub.add_parser("connect", help="connect to Mark VI as a peer")
    p_conn.add_argument("--agent", default=os.environ.get("FORGE_AGENT", "optimus"))

    sub.add_parser("agents", help="list configured agents")

    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("FORGE_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "demo":
        from forge.demo import run_demo
        return asyncio.run(run_demo())

    if args.cmd == "agents":
        from forge.agents.registry import AgentRegistry
        reg = AgentRegistry.load()
        for aid in reg.ids():
            cfg = reg.get(aid)
            print(f"{aid:12} {cfg.name:10} model={cfg.model_ref:20} tools={list(cfg.tool_names)}")
        return 0

    if args.cmd == "serve":
        from forge.config import ForgeSettings
        from forge.agents.registry import AgentRegistry
        from forge.gate.server import serve
        try:
            asyncio.run(serve(args.host, args.port,
                              settings=ForgeSettings.from_env(), registry=AgentRegistry.load()))
        except KeyboardInterrupt:
            pass
        return 0

    if args.cmd == "connect":
        os.environ["FORGE_AGENT"] = args.agent
        from forge.gate.peer import main as peer_main
        return peer_main()

    return 1


if __name__ == "__main__":
    sys.exit(main())
