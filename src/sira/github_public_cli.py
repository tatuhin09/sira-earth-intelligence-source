from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    ROOT = Path(__file__).resolve().parents[2]
    SRC = ROOT / "src"
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))

from sira.github_public import (
    GitHubPublicClient,
    GitHubPublicError,
    GitHubRateLimitError,
)


DEFAULT_CACHE = Path("runtime/cache/github-public")


def _client(cache_dir: str | Path) -> GitHubPublicClient:
    return GitHubPublicClient(Path(cache_dir))


def _json_print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _rate_limit_dict(result) -> dict[str, Any]:
    return {
        "limit": result.rate_limit.limit,
        "remaining": result.rate_limit.remaining,
        "used": result.rate_limit.used,
        "reset_at": result.rate_limit.reset_at,
        "resource": result.rate_limit.resource,
        "retry_after": result.rate_limit.retry_after,
    }


def _safe_repo_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "full_name": item.get("full_name"),
        "description": item.get("description"),
        "html_url": item.get("html_url"),
        "language": item.get("language"),
        "stargazers_count": item.get("stargazers_count"),
        "forks_count": item.get("forks_count"),
        "updated_at": item.get("updated_at"),
        "archived": item.get("archived"),
        "fork": item.get("fork"),
    }


def _search(client: GitHubPublicClient, args: argparse.Namespace) -> int:
    result = client.search_repositories(
        args.query,
        per_page=args.limit,
        sort=args.sort,
        order=args.order,
    )
    payload = result.data if isinstance(result.data, dict) else {}
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    _json_print(
        {
            "status": "ok",
            "command": "search",
            "query": args.query,
            "total_count": payload.get("total_count"),
            "items": [
                _safe_repo_item(item)
                for item in items[: args.limit]
                if isinstance(item, dict)
            ],
            "from_cache": result.from_cache,
            "rate_limit": _rate_limit_dict(result),
        }
    )
    return 0


def _repo(client: GitHubPublicClient, args: argparse.Namespace) -> int:
    result = client.get_repository(args.owner, args.repo)
    data = result.data if isinstance(result.data, dict) else {}
    _json_print(
        {
            "status": "ok",
            "command": "repo",
            "repository": _safe_repo_item(data),
            "default_branch": data.get("default_branch"),
            "license": (
                data.get("license", {}).get("spdx_id")
                if isinstance(data.get("license"), dict)
                else None
            ),
            "open_issues_count": data.get("open_issues_count"),
            "size_kb": data.get("size"),
            "from_cache": result.from_cache,
            "rate_limit": _rate_limit_dict(result),
        }
    )
    return 0


def _file(client: GitHubPublicClient, args: argparse.Namespace) -> int:
    result = client.get_text_file(
        args.owner,
        args.repo,
        args.path,
        ref=args.ref,
    )
    text = result.data if isinstance(result.data, str) else str(result.data)
    truncated = len(text) > args.max_chars
    shown = text[: args.max_chars]
    _json_print(
        {
            "status": "ok",
            "command": "file",
            "owner": args.owner,
            "repo": args.repo,
            "path": args.path,
            "ref": args.ref,
            "text": shown,
            "truncated": truncated,
            "returned_chars": len(shown),
            "from_cache": result.from_cache,
            "rate_limit": _rate_limit_dict(result),
        }
    )
    return 0


def _rate(client: GitHubPublicClient, args: argparse.Namespace) -> int:
    result = client.rate_limit()
    data = result.data if isinstance(result.data, dict) else {}
    resources = data.get("resources") if isinstance(data.get("resources"), dict) else {}
    safe_resources = {}
    for name, value in resources.items():
        if not isinstance(value, dict):
            continue
        safe_resources[name] = {
            "limit": value.get("limit"),
            "remaining": value.get("remaining"),
            "reset": value.get("reset"),
            "used": value.get("used"),
        }
    _json_print(
        {
            "status": "ok",
            "command": "rate-limit",
            "resources": safe_resources,
            "rate_limit": _rate_limit_dict(result),
        }
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded read-only GitHub public research CLI for SIRA."
    )
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE),
        help="Local cache directory (default: runtime/cache/github-public).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="Search public repositories.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5, choices=range(1, 21))
    search.add_argument(
        "--sort",
        choices=("stars", "forks", "help-wanted-issues", "updated"),
    )
    search.add_argument("--order", choices=("asc", "desc"), default="desc")
    search.set_defaults(handler=_search)

    repo = sub.add_parser("repo", help="Read public repository metadata.")
    repo.add_argument("owner")
    repo.add_argument("repo")
    repo.set_defaults(handler=_repo)

    file_cmd = sub.add_parser("file", help="Read a bounded public text file.")
    file_cmd.add_argument("owner")
    file_cmd.add_argument("repo")
    file_cmd.add_argument("path")
    file_cmd.add_argument("--ref")
    file_cmd.add_argument("--max-chars", type=int, default=12000)
    file_cmd.set_defaults(handler=_file)

    rate = sub.add_parser("rate-limit", help="Inspect GitHub REST rate-limit state.")
    rate.set_defaults(handler=_rate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if getattr(args, "max_chars", 1) <= 0 or getattr(args, "max_chars", 1) > 50000:
        parser.error("--max-chars must be between 1 and 50000")

    client = _client(args.cache_dir)

    try:
        return int(args.handler(client, args))
    except GitHubRateLimitError as exc:
        _json_print(
            {
                "status": "rate_limited",
                "error": "github_rate_limit",
                "http_status": exc.status,
                "remaining": exc.remaining,
                "reset_at": exc.reset_at,
                "retry_after": exc.retry_after,
                "resource": exc.resource,
            }
        )
        return 3
    except (GitHubPublicError, ValueError, UnicodeDecodeError) as exc:
        _json_print(
            {
                "status": "error",
                "error": type(exc).__name__,
                "message": str(exc),
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
