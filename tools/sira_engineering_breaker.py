#!/usr/bin/env python3
"""Owner control for the engineering failure circuit breaker (default: disabled).

    python tools/sira_engineering_breaker.py status
    python tools/sira_engineering_breaker.py enable [--threshold 5] [--max-hold-hours 24]
    python tools/sira_engineering_breaker.py disable

When enabled, an opportunity type (for example `complex_function`) whose last N attempts
all failed (rejected or structural goal not met) is hidden from discovery for 1h, 2h, 4h ...
up to --max-hold-hours. One promoted result resets the streak. Derived from saved handoffs,
so there is no extra state to repair.
"""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.engineering_failure_breaker import breaker_state, load_policy, write_policy  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    enable = commands.add_parser("enable")
    enable.add_argument("--threshold", type=int, default=5)
    enable.add_argument("--max-hold-hours", type=int, default=24)
    commands.add_parser("disable")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.command == "enable":
        policy = write_policy(root, enabled=True, failure_threshold=args.threshold,
                              max_hold_hours=args.max_hold_hours)
    elif args.command == "disable":
        current = load_policy(root)
        policy = write_policy(root, enabled=False,
                              failure_threshold=current["failure_threshold"],
                              max_hold_hours=current["max_hold_hours"])
    else:
        policy = load_policy(root)
    state = breaker_state(root, now_epoch=time.time())
    print(json.dumps({"policy": {k: policy[k] for k in (
        "enabled", "failure_threshold", "max_hold_hours", "valid", "source")},
        "open_breakers": state}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
