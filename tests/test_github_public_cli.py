from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sira.github_public import GitHubRateLimitError, GitHubResult, RateLimit
from sira.github_public_cli import main


RATE = RateLimit(
    limit=60,
    remaining=59,
    used=1,
    reset_at=2000000000,
    resource="core",
)


class FakeClient:
    def search_repositories(self, query, *, per_page, sort, order):
        return GitHubResult(
            data={
                "total_count": 1,
                "items": [
                    {
                        "full_name": "owner/repo",
                        "description": "demo",
                        "html_url": "https://github.com/owner/repo",
                        "language": "Python",
                        "stargazers_count": 10,
                        "forks_count": 2,
                        "updated_at": "2026-01-01T00:00:00Z",
                        "archived": False,
                        "fork": False,
                    }
                ],
            },
            rate_limit=RATE,
            from_cache=False,
            etag='"x"',
        )

    def get_repository(self, owner, repo):
        return GitHubResult(
            data={
                "full_name": f"{owner}/{repo}",
                "description": "demo",
                "html_url": f"https://github.com/{owner}/{repo}",
                "language": "Python",
                "stargazers_count": 10,
                "forks_count": 2,
                "updated_at": "2026-01-01T00:00:00Z",
                "archived": False,
                "fork": False,
                "default_branch": "main",
                "license": {"spdx_id": "MIT"},
                "open_issues_count": 3,
                "size": 100,
            },
            rate_limit=RATE,
            from_cache=True,
            etag='"x"',
        )

    def get_text_file(self, owner, repo, path, *, ref=None):
        return GitHubResult(
            data="abcdefghijklmnopqrstuvwxyz",
            rate_limit=RATE,
            from_cache=True,
            etag='"x"',
        )

    def rate_limit(self):
        return GitHubResult(
            data={
                "resources": {
                    "core": {"limit": 60, "remaining": 59, "reset": 2000000000, "used": 1}
                }
            },
            rate_limit=RATE,
            from_cache=False,
            etag=None,
        )


def run_cli(argv):
    stdout = io.StringIO()
    with patch("sira.github_public_cli._client", return_value=FakeClient()):
        with contextlib.redirect_stdout(stdout):
            rc = main(argv)
    return rc, json.loads(stdout.getvalue())


class GitHubPublicCliTests(unittest.TestCase):
    def test_search_outputs_bounded_safe_json(self):
        rc, payload = run_cli(["search", "agent memory", "--limit", "3"])
        self.assertEqual(rc, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["command"], "search")
        self.assertEqual(payload["items"][0]["full_name"], "owner/repo")
        self.assertNotIn("owner", payload["items"][0])
        self.assertEqual(payload["rate_limit"]["remaining"], 59)

    def test_repo_outputs_curated_metadata(self):
        rc, payload = run_cli(["repo", "owner", "repo"])
        self.assertEqual(rc, 0)
        self.assertEqual(payload["repository"]["full_name"], "owner/repo")
        self.assertEqual(payload["license"], "MIT")
        self.assertEqual(payload["default_branch"], "main")

    def test_file_output_is_character_bounded(self):
        rc, payload = run_cli(
            ["file", "owner", "repo", "README.md", "--max-chars", "5"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(payload["text"], "abcde")
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["returned_chars"], 5)

    def test_rate_limit_outputs_curated_resource_state(self):
        rc, payload = run_cli(["rate-limit"])
        self.assertEqual(rc, 0)
        self.assertEqual(payload["resources"]["core"]["remaining"], 59)

    def test_rate_limit_error_is_json_and_nonzero(self):
        class LimitedClient(FakeClient):
            def get_repository(self, owner, repo):
                raise GitHubRateLimitError(
                    "limited",
                    status=403,
                    remaining=0,
                    reset_at=2000000000,
                    retry_after=None,
                    resource="core",
                )

        stdout = io.StringIO()
        with patch("sira.github_public_cli._client", return_value=LimitedClient()):
            with contextlib.redirect_stdout(stdout):
                rc = main(["repo", "owner", "repo"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 3)
        self.assertEqual(payload["status"], "rate_limited")
        self.assertEqual(payload["remaining"], 0)

    def test_invalid_max_chars_is_rejected_before_client_use(self):
        stderr = io.StringIO()
        with patch("sira.github_public_cli._client") as factory:
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    main(
                        [
                            "file",
                            "owner",
                            "repo",
                            "README.md",
                            "--max-chars",
                            "50001",
                        ]
                    )
        self.assertEqual(ctx.exception.code, 2)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
