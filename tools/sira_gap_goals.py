#!/usr/bin/env python3
"""Owner control for gap-driven learning goals (default: disabled).

    python tools/sira_gap_goals.py status
    python tools/sira_gap_goals.py enable [--max-active 2] [--cooldown-days 14] [--local-only]
    python tools/sira_gap_goals.py disable

When enabled, SIRA turns capability gaps from its self-model into a small number of
public-research learning goals with short study plans so the autonomous loop has
fresh work. Generated goals are listed in memory/gap_goals/registry.json with the
reason they were created and can be paused any time with `sira goals pause <id>`.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.gap_goal_generator import _load_registry, load_policy, write_policy  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    enable = commands.add_parser("enable")
    enable.add_argument("--max-active", type=int, default=2)
    enable.add_argument("--cooldown-days", type=int, default=14)
    enable.add_argument("--local-only", action="store_true",
                        help="generated goals may not use public research")
    commands.add_parser("disable")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.command == "enable":
        policy = write_policy(root, enabled=True, max_active=args.max_active,
                              capability_cooldown_days=args.cooldown_days,
                              public_research_allowed=not args.local_only)
    elif args.command == "disable":
        current = load_policy(root)
        policy = write_policy(root, enabled=False, max_active=current["max_active"],
                              capability_cooldown_days=current["capability_cooldown_days"],
                              public_research_allowed=current["public_research_allowed"])
    else:
        policy = load_policy(root)
    registry = _load_registry(root)
    goals = [] if registry is None else [
        {k: g.get(k) for k in ("goal_id", "state", "topic", "capability", "gap_reason")}
        for g in registry["goals"]]
    print(json.dumps({"policy": {k: policy[k] for k in (
        "enabled", "max_active", "capability_cooldown_days", "public_research_allowed",
        "valid", "source")}, "registry_readable": registry is not None,
        "generated_goals": goals}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
