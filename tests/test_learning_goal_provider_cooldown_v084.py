"""A rate-limited public provider is not retried for every owner goal."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sira.learning_goal_runtime import _default_goal_open_access_search


class LearningGoalProviderCooldownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_rate_limit_is_shared_across_topics_and_expires(self):
        calls = []

        def search(_root, _query, *, providers, **_kwargs):
            names = [provider.name for provider in providers]
            calls.append(names)
            return {
                "status": "partial", "results": [{"title": "A paper"}],
                "providers_attempted": names,
                "provider_failures": ([{"provider": "arxiv", "code": "http_429",
                                       "retry_after_seconds": 120}]
                                      if len(calls) == 1 else []),
                "metrics": {"api_requests": len(names), "cache_hit": False},
            }

        with patch("sira.learning_goal_runtime.search_open_access_free", side_effect=search):
            _default_goal_open_access_search(self.root, "Cybersecurity", now_epoch=2_000_000_000)
            second = _default_goal_open_access_search(self.root, "Ethical hacking",
                                                      now_epoch=2_000_000_119)
            _default_goal_open_access_search(self.root, "White hat security",
                                             now_epoch=2_000_000_121)

        self.assertEqual(calls, [["doaj", "arxiv"], ["doaj"], ["doaj", "arxiv"]])
        self.assertEqual(second["metrics"]["api_requests"], 1)
        self.assertEqual(second["providers_skipped_cooldown"][0]["provider"], "arxiv")

    def test_both_rate_limited_providers_defer_without_network(self):
        calls = []

        def search(_root, _query, *, providers, **_kwargs):
            names = [provider.name for provider in providers]
            calls.append(names)
            return {
                "status": "failed", "results": [], "providers_attempted": names,
                "provider_failures": [
                    {"provider": name, "code": "http_429", "retry_after_seconds": None}
                    for name in names
                ],
                "metrics": {"api_requests": len(names), "cache_hit": False},
            }

        with patch("sira.learning_goal_runtime.search_open_access_free", side_effect=search):
            _default_goal_open_access_search(self.root, "Cybersecurity", now_epoch=2_000_000_000)
            deferred = _default_goal_open_access_search(self.root, "Penetration testing",
                                                        now_epoch=2_000_000_060)

        self.assertEqual(calls, [["doaj", "arxiv"]])
        self.assertEqual(deferred["status"], "deferred_provider_cooldown")
        self.assertEqual(deferred["results"], [])
        self.assertEqual(deferred["metrics"]["api_requests"], 0)
        self.assertEqual({row["provider"] for row in deferred["providers_skipped_cooldown"]},
                         {"doaj", "arxiv"})

    def test_cached_failures_do_not_extend_cooldown(self):
        calls = []

        def search(_root, _query, *, providers, **_kwargs):
            calls.append([provider.name for provider in providers])
            return {
                "status": "partial", "results": [{"title": "A paper"}],
                "providers_attempted": calls[-1],
                "provider_failures": [{"provider": "arxiv", "code": "http_429",
                                       "retry_after_seconds": 90}],
                "metrics": {"api_requests": 0, "cache_hit": True},
            }

        with patch("sira.learning_goal_runtime.search_open_access_free", side_effect=search):
            _default_goal_open_access_search(self.root, "Cybersecurity", now_epoch=2_000_000_000)
            _default_goal_open_access_search(self.root, "Ethical hacking", now_epoch=2_000_000_020)

        self.assertEqual(calls, [["doaj", "arxiv"], ["doaj", "arxiv"]])

    def test_corrupt_cooldown_state_blocks_public_provider_requests(self):
        directory = self.root / ".cache"
        directory.mkdir()
        (directory / "learning_goal_provider_cooldowns.json").write_text("{broken", encoding="utf-8")
        with patch("sira.learning_goal_runtime.search_open_access_free") as search:
            with self.assertRaises(ValueError):
                _default_goal_open_access_search(self.root, "Cybersecurity")
        search.assert_not_called()

    def test_malformed_provider_failure_does_not_crash_runtime_with_type_error(self):
        malformed = {
            "status": "failed", "results": [],
            "providers_attempted": ["arxiv"],
            "provider_failures": [{"provider": "arxiv", "code": []}],
            "metrics": {"api_requests": 1, "cache_hit": False},
        }
        with patch("sira.learning_goal_runtime.search_open_access_free", return_value=malformed):
            with self.assertRaises(ValueError):
                _default_goal_open_access_search(self.root, "Cybersecurity")


if __name__ == "__main__":
    unittest.main()
