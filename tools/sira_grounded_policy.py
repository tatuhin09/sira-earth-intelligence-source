#!/usr/bin/env python3
"""Owner control for grounded-claim learning (default: disabled).

    python tools/sira_grounded_policy.py status
    python tools/sira_grounded_policy.py enable --daily 20
    python tools/sira_grounded_policy.py disable

Enabling lets learning goals send fetched PUBLIC passages and the goal's study question
to the configured Gemini model, at most --daily requests per UTC day. Nothing else changes.
"""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.learning_goal_grounded_claims import load_policy, write_policy  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    enable = commands.add_parser("enable")
    enable.add_argument("--daily", type=int, default=20)
    commands.add_parser("disable")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.command == "enable":
        policy = write_policy(root, enabled=True, daily_model_requests=args.daily)
    elif args.command == "disable":
        policy = write_policy(root, enabled=False, daily_model_requests=0)
    else:
        policy = load_policy(root)
    print({"enabled": policy["enabled"], "daily_model_requests": policy["daily_model_requests"],
           "valid": policy["valid"], "source": policy["source"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
