#!/usr/bin/env python3
"""Owner control for budgeted web-search discovery and trusted documentation hosts.

    python tools/sira_web_discovery.py status
    python tools/sira_web_discovery.py enable [--monthly 1000] [--reserve 100] [--max-results 3]
    python tools/sira_web_discovery.py disable
    python tools/sira_web_discovery.py hosts list|seed
    python tools/sira_web_discovery.py hosts add docs.example.org [more ...]
    python tools/sira_web_discovery.py hosts remove docs.example.org

Enabled (default: disabled): learning goals also search the Tavily API, restricted to the
trusted hosts below, spending at most monthly - reserve credits per month (spread evenly
over the days left). Only SIRA's own use is counted locally; the reserve keeps your own
`research` commands working. Nothing here grants authority or spends money.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.trusted_hosts import (  # noqa: E402
    SEED_HOSTS, add_hosts, load_registry, remove_hosts,
)
from sira.web_search_discovery import allowed_domains, budget_status, write_policy  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    enable = commands.add_parser("enable")
    enable.add_argument("--monthly", type=int, default=1000)
    enable.add_argument("--reserve", type=int, default=100)
    enable.add_argument("--max-results", type=int, default=3)
    commands.add_parser("disable")
    hosts = commands.add_parser("hosts")
    hosts.add_argument("action", choices=["list", "seed", "add", "remove"])
    hosts.add_argument("names", nargs="*")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.command == "enable":
        write_policy(root, enabled=True, monthly_credits=args.monthly,
                     owner_reserve=args.reserve, max_results=args.max_results)
    elif args.command == "disable":
        current = budget_status(root)["policy"]
        write_policy(root, enabled=False, monthly_credits=current["monthly_credits"],
                     owner_reserve=current["owner_reserve"], max_results=current["max_results"])
    elif args.command == "hosts":
        if args.action == "seed":
            add_hosts(root, SEED_HOSTS)
        elif args.action == "add":
            add_hosts(root, args.names)
        elif args.action == "remove":
            remove_hosts(root, args.names)
    registry = load_registry(root)
    print(json.dumps({"budget": budget_status(root), "trusted_hosts": registry["hosts"],
                      "registry_valid": registry["valid"],
                      "search_restricted_to": allowed_domains(root)}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
