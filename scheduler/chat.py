"""Interactive scheduling conversation. Synthetic clinic, fixed clock."""

from __future__ import annotations

import argparse
import sys

from scheduler.agent import Agent, Session
from scheduler.clinic import NOW, fresh_clinic
from scheduler.policy import V0_PATH, load_policy
from scheduler.tools import ToolBelt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Talk to the clinic scheduling agent.")
    parser.add_argument(
        "--policy",
        default=str(V0_PATH),
        help="Policy JSON. Use policies/v0.json before the loop and policies/v1.json after it.",
    )
    args = parser.parse_args(argv)
    policy = load_policy(args.policy)
    _repl(policy, args.policy)
    return 0


def _repl(policy: dict, policy_path: str) -> None:
    clinic = fresh_clinic()
    tools = ToolBelt(clinic, policy)
    agent = Agent(policy, tools)
    session = Session()
    print("Clinic scheduling agent. Synthetic patients only. Not a real clinic.")
    print(f"Policy v{policy['version']} ({policy_path})")
    print(f"Clinic clock: {NOW.strftime('%Y-%m-%d %H:%M')}")
    print("Commands: /reset  /trace  /quit")
    print("Try: book a physical, ask Maya Chen for her appointment, or describe chest pain.")
    print()
    while True:
        try:
            user_text = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not user_text:
            continue
        if user_text in {"/quit", "/exit"}:
            return
        if user_text == "/reset":
            clinic = fresh_clinic()
            tools = ToolBelt(clinic, policy)
            agent = Agent(policy, tools)
            session = Session()
            print("Session reset.")
            continue
        if user_text == "/trace":
            if not tools.trace:
                print("No tool calls yet.")
            for row in tools.trace:
                status = "ok" if row["ok"] else "ERROR"
                print(f"  {row['name']} {status} - {row['detail']}")
            continue
        start = len(tools.trace)
        turn = agent.handle(session, user_text)
        for row in tools.trace[start:]:
            status = "ok" if row["ok"] else "ERROR"
            print(f"  [tool] {row['name']} {status} - {row['detail']}")
        print(f"Agent: {turn.text}")
        print()


if __name__ == "__main__":
    sys.exit(main())
