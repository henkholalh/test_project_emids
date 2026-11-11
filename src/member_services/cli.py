"""Terminal client for one rep conversation (development and demos).

    python -m member_services.cli --tenant plan_a --member M000000
    python -m member_services.cli --tenant plan_a --member M000000 -m "What is the specialist copay?"

With ``-m`` (repeatable) the messages are sent in order and the program exits; otherwise it reads rep messages
from the prompt until ``/quit``. Type ``/confirm`` to confirm the pending action explicitly, ``/trace`` to print
the redacted trace. The CLI trusts the ``--tenant`` flag, so it is a developer tool; the HTTP API takes the
tenant from the rep's credential.
"""
from __future__ import annotations

import argparse
import json
import sys

from .agent.agent import Agent
from .config import ConfigError, get_settings
from .context import SessionContext
from .gateway.factory import build_gateway
from .llm import make_llm


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Member services assistant (CLI)")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--member", required=True)
    ap.add_argument("-m", "--message", action="append", help="send this rep message (repeatable)")
    ap.add_argument("--show-usage", action="store_true", help="print model calls, tools, cost and latency")
    args = ap.parse_args(argv)
    try:
        settings = get_settings()
        settings.require_runtime()
        gw = build_gateway(settings, SessionContext(args.tenant, args.member))
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    gw.prefetch()
    agent = Agent(gw, make_llm(settings))
    print(f"[{gw.kb.client_name} | member {'found' if gw.member_found else 'NOT found'} | model "
          f"{agent.llm.model}]")

    def send(text: str, confirm: str | None = None) -> None:
        r = agent.turn(text, confirm)
        print(f"\nassistant> {r.answer}")
        if r.pending_action:
            print(f"  [pending {r.pending_action['type']} {r.pending_action['pending_action_id']}: "
                  f"{r.pending_action['category']}] confirm with the member, then reply or type /confirm")
        if r.filed_grievance:
            print(f"  [filed {r.filed_grievance['grievance_id']}]")
        if args.show_usage:
            print(f"  [{r.stopped} | model calls {r.model_calls} | tools {r.tool_calls} | ${r.cost_usd:.4f} | "
                  f"{r.latency_ms} ms]")

    if args.message:
        for m in args.message:
            print(f"\nrep> {m}")
            send(m)
        return 0
    while True:
        try:
            text = input("\nrep> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if text in ("/quit", "/exit"):
            return 0
        if text == "/trace":
            print(json.dumps(gw.tracer.events, indent=1, default=str))
            continue
        if text == "/confirm":
            p = gw.grievances.pending
            send("Confirmed.", p.action_id if p else None)
            continue
        if text:
            send(text)


if __name__ == "__main__":
    raise SystemExit(main())
