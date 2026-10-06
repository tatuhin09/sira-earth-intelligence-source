"""Budgeted trusted-domain web discovery and the owner trusted-host registry."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.learning_goal_general_engine import (
    _classify, assess_general_documents, has_general_source_gap, run_general_learning,
)
from sira.learning_goal_runtime import _with_web_discovery
from sira.learning_goals import LearningGoalStore
from sira.models import ProviderError, Source
from sira.trusted_hosts import (
    MAX_HOSTS, SEED_HOSTS, add_hosts, load_registry, load_trusted_hosts, normalize_host,
    remove_hosts, write_registry,
)
from sira.web_search_discovery import (
    BASE_HOSTS, allowed_domains, budget_status, load_policy, maybe_search_trusted_web,
    search_trusted_web, write_policy,
)

DAY1 = 1_790_000_000.0  # 2026-09-21 (UTC) - mid month
QUERY = "Pytest fixtures and test discovery"


def source(url, title="Pytest fixtures", text="Fixtures provide test setup in pytest."):
    return Source(title, url, text, "2026-10-01T00:00:00+00:00")


class FakeSearch:
    def __init__(self, sources=None, credits=1, error=None):
        self.calls, self.sources, self.credits, self.error = [], sources, credits, error

    def __call__(self, root, query, max_results, domains):
        self.calls.append({"query": query, "max_results": max_results, "domains": list(domains)})
        if self.error:
            raise self.error
        sources = self.sources if self.sources is not None else [
            source("https://docs.pytest.org/en/stable/fixture.html")]
        return sources, self.credits


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_hostnames_are_normalized_and_unsafe_values_rejected(self):
        self.assertEqual(normalize_host(" Docs.Python.ORG. "), "docs.python.org")
        for bad in ("http://docs.python.org", "docs.python.org/path", "docs.python.org:8443",
                    "*.python.org", "127.0.0.1", "localhost", "intranet.local", "a", "-x.com",
                    "user@docs.python.org", "", 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_host(bad)

    def test_registry_round_trip_add_remove_and_seed(self):
        self.assertEqual(load_registry(self.root)["hosts"], [])
        add_hosts(self.root, ["docs.pytest.org", "Docs.Python.org"])
        self.assertEqual(load_trusted_hosts(self.root),
                         frozenset({"docs.pytest.org", "docs.python.org"}))
        remove_hosts(self.root, ["docs.pytest.org"])
        self.assertEqual(load_registry(self.root)["hosts"], ["docs.python.org"])
        add_hosts(self.root, SEED_HOSTS)
        self.assertEqual(len(load_registry(self.root)["hosts"]), len(set(SEED_HOSTS)))
        with self.assertRaises(ValueError):
            write_registry(self.root, [f"h{i}.example.org" for i in range(MAX_HOSTS + 1)])

    def test_malformed_registry_grants_nothing_and_blocks_edits(self):
        add_hosts(self.root, ["docs.pytest.org"])
        path = self.root / "memory/trusted_hosts/registry.json"
        for broken in ("{bad", json.dumps({"schema": "x", "hosts": []}),
                       json.dumps({"schema": "sira.trusted_hosts.v1", "hosts": ["http://x.com"]})):
            path.write_text(broken)
            self.assertFalse(load_registry(self.root)["valid"])
            self.assertEqual(load_trusted_hosts(self.root), frozenset())
            with self.assertRaises(ValueError):
                add_hosts(self.root, ["docs.python.org"])


class EngineTests(unittest.TestCase):
    QUESTION = "Pytest fixtures and test setup"
    SENTENCE = "Pytest fixtures provide reliable test setup for every test function."

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        store = LearningGoalStore(self.root)
        self.goal = store.create("Python testing tools", public_research_allowed=True)
        store.set_plan(self.goal["goal_id"], [self.QUESTION, "Test discovery in pytest"])
        self.urls = ("https://docs.pytest.org/en/stable/fixture.html",
                     "https://docs.python.org/3/library/unittest.html")
        self.research = {"knowledge": {"results": [{"title": self.QUESTION, "url": self.urls[0]}]},
                         "open_access": {"results": [{"title": self.QUESTION, "url": self.urls[1]}]}}

    def fetcher(self, url):
        return url, "text/html", f"<html><main><p>{self.SENTENCE}</p></main></html>".encode()

    def test_classification_adds_owner_trusted_without_changing_existing_classes(self):
        self.assertIsNone(_classify("docs.pytest.org"))
        self.assertEqual(_classify("docs.pytest.org", frozenset({"docs.pytest.org"})),
                         "owner_trusted")
        self.assertEqual(_classify("www.nasa.gov", frozenset()), "official_primary")
        self.assertEqual(_classify("arxiv.org"), "scholarly_public")
        self.assertEqual(_classify("en.wikipedia.org"), "public_reference")

    def test_unregistered_hosts_are_still_unreadable(self):
        run = run_general_learning(self.root, self.goal["goal_id"], self.research,
                                   question=self.QUESTION, fetcher=self.fetcher)
        self.assertEqual(run["status"], "insufficient_independent_sources")
        self.assertEqual(run["documents"], [])
        self.assertFalse(has_general_source_gap(self.QUESTION, self.research))

    def test_registered_hosts_become_readable_and_can_verify_by_exact_text(self):
        add_hosts(self.root, ["docs.pytest.org", "docs.python.org"])
        trusted = load_trusted_hosts(self.root)
        self.assertTrue(has_general_source_gap(self.QUESTION, self.research, trusted))
        run = run_general_learning(self.root, self.goal["goal_id"], self.research,
                                   question=self.QUESTION, fetcher=self.fetcher)
        self.assertEqual(run["status"], "verified_knowledge_recorded", run)
        self.assertEqual(run["source_classes"], ["owner_trusted", "owner_trusted"])
        self.assertEqual(run["resource_usage"]["paid_requests"], 0)

    def test_assessment_accepts_trusted_documents_only_when_told_the_trust_set(self):
        text = self.SENTENCE
        docs = [{"url": u, "host": u.split("/")[2], "title": "t", "text": text,
                 "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                 "retrieved_at": "2099-01-01T00:00:00+00:00", "verified": False}
                for u in self.urls]
        with self.assertRaises(ValueError):
            assess_general_documents(self.QUESTION, docs, now="2099-01-01T00:00:10+00:00")
        decision = assess_general_documents(self.QUESTION, docs, now="2099-01-01T00:00:10+00:00",
                                            trusted_hosts=frozenset({"docs.pytest.org",
                                                                     "docs.python.org"}))
        self.assertEqual(decision["status"], "corroborated")


class WebDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_policy(self.root, enabled=True, monthly_credits=1000, owner_reserve=100)
        add_hosts(self.root, ["docs.pytest.org"])

    def search(self, fake, at=DAY1, query=QUERY):
        return search_trusted_web(self.root, query, now_epoch=at, searcher=fake)

    def test_disabled_by_default_never_searches(self):
        (self.root / "memory/web_search/policy.json").unlink()
        fake = FakeSearch()
        result = self.search(fake)
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(fake.calls, [])

    def test_search_is_restricted_to_readable_hosts_and_filters_results(self):
        fake = FakeSearch([source("https://docs.pytest.org/en/stable/fixture.html"),
                           source("https://spam.example.com/pytest"),
                           source("https://en.wikipedia.org/wiki/Pytest")])
        result = self.search(fake)
        self.assertEqual(result["status"], "searched")
        self.assertEqual(set(fake.calls[0]["domains"]), {*BASE_HOSTS, "docs.pytest.org"})
        self.assertEqual({r["url"].split("/")[2] for r in result["rows"]},
                         {"docs.pytest.org", "en.wikipedia.org"})
        row = result["rows"][0]
        self.assertTrue({"paper_id", "title", "abstract", "url", "provider"} <= set(row))
        self.assertEqual(row["provider"], "tavily")
        for key in ("paid_spending", "authority_granted", "promotion_performed"):
            self.assertIs(result[key], False)
        self.assertEqual(result["model_requests"], 0)

    def test_budget_reserve_daily_allowance_and_next_day(self):
        status = budget_status(self.root, now_epoch=DAY1)
        self.assertEqual(status["autonomous_budget"], 900)
        allowance = status["daily_allowance"]
        self.assertGreaterEqual(allowance, 30)
        for index in range(allowance):
            self.assertEqual(self.search(FakeSearch(), query=f"query number {index}")["status"],
                             "searched")
        blocked = self.search(FakeSearch(), query="one more question")
        self.assertEqual(blocked["status"], "daily_budget_exhausted")
        self.assertEqual(self.search(FakeSearch(), at=DAY1 + 86_400,
                                     query="one more question")["status"], "searched")

    def test_monthly_budget_exhausted_and_month_rollover(self):
        ledger = self.root / "memory/web_search/usage.json"
        month = budget_status(self.root, now_epoch=DAY1)["month"]
        ledger.write_text(json.dumps({"month": month, "credits_used": 900, "requests": 900,
                                      "day": "x", "day_credits": 0, "paused_until": 0}))
        self.assertEqual(self.search(FakeSearch())["status"], "monthly_budget_exhausted")
        self.assertEqual(self.search(FakeSearch(), at=DAY1 + 40 * 86_400)["status"], "searched")

    def test_cache_prevents_repeat_spend_until_it_expires(self):
        fake = FakeSearch()
        self.assertEqual(self.search(fake)["status"], "searched")
        again = self.search(fake, at=DAY1 + 3600)
        self.assertEqual(again["status"], "cache_hit")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(budget_status(self.root, now_epoch=DAY1 + 3600)["used"], 1)
        late = self.search(fake, at=DAY1 + 8 * 86_400)
        self.assertEqual(late["status"], "searched")
        self.assertEqual(len(fake.calls), 2)

    def test_reported_higher_cost_is_counted(self):
        self.search(FakeSearch(credits=2))
        self.assertEqual(budget_status(self.root, now_epoch=DAY1)["used"], 2)

    def test_provider_error_counts_pauses_and_recovers(self):
        error = ProviderError("http_429", True, 120)
        result = self.search(FakeSearch(error=error))
        self.assertEqual((result["status"], result["reason"]), ("provider_error", "http_429"))
        self.assertEqual(budget_status(self.root, now_epoch=DAY1)["used"], 1)
        fake = FakeSearch()
        self.assertEqual(self.search(fake, at=DAY1 + 60, query="another")["status"],
                         "provider_paused")
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.search(fake, at=DAY1 + 121, query="another")["status"], "searched")

    def test_unsent_request_and_missing_key_release_the_reservation(self):
        self.search(FakeSearch(error=ProviderError("invalid_response", False)))
        self.assertEqual(budget_status(self.root, now_epoch=DAY1)["used"], 0)
        self.search(FakeSearch(error=ValueError("TAVILY_API_KEY missing")), query="other")
        self.assertEqual(budget_status(self.root, now_epoch=DAY1)["used"], 0)

    def test_policy_is_bounded_and_malformed_policy_fails_closed(self):
        for kwargs in ({"monthly_credits": 0}, {"owner_reserve": 1000}, {"max_results": 9},
                       {"monthly_credits": 99_999}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                write_policy(self.root, enabled=True, **kwargs)
        (self.root / "memory/web_search/policy.json").write_text("{bad")
        self.assertFalse(load_policy(self.root)["valid"])
        fake = FakeSearch()
        self.assertEqual(self.search(fake)["status"], "policy_invalid")
        self.assertEqual(fake.calls, [])

    def test_state_files_never_contain_credentials(self):
        self.search(FakeSearch())
        text = "".join(p.read_text() for p in (self.root / "memory/web_search").rglob("*.json"))
        self.assertNotIn("tvly", text)
        self.assertNotIn("Bearer", text)

    def test_wrapper_never_raises(self):
        def boom(*_a):
            raise RuntimeError("boom")
        result = maybe_search_trusted_web(self.root, QUERY, now_epoch=DAY1, searcher=boom)
        self.assertEqual(result["status"], "web_search_error")

    def test_allowed_domains_are_bounded_and_include_base_hosts(self):
        domains = allowed_domains(self.root)
        self.assertTrue(set(BASE_HOSTS) <= set(domains))
        self.assertLessEqual(len(domains), 60)


class RuntimeMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = {"status": "completed", "results": [{"url": "https://arxiv.org/abs/1", "title": "a"}],
                     "metrics": {"api_requests": 2, "result_count": 1, "cache_hit": False}}

    def test_disabled_discovery_leaves_the_report_unchanged(self):
        self.assertEqual(_with_web_discovery(self.root, QUERY, dict(self.base), DAY1), self.base)

    def test_enabled_discovery_merges_unique_rows_and_counts_requests(self):
        write_policy(self.root, enabled=True)
        add_hosts(self.root, ["docs.pytest.org"])
        rows = [{"url": "https://arxiv.org/abs/1", "title": "dup"},
                {"url": "https://docs.pytest.org/x", "title": "new"}]
        with patch("sira.learning_goal_runtime.maybe_search_trusted_web",
                   return_value={"status": "searched", "rows": rows, "credits_used": 1.0,
                                 "api_requests": 1, "paid_spending": False}):
            merged = _with_web_discovery(self.root, QUERY, dict(self.base), DAY1)
        self.assertEqual([r["url"] for r in merged["results"]],
                         ["https://arxiv.org/abs/1", "https://docs.pytest.org/x"])
        self.assertEqual(merged["metrics"]["api_requests"], 3)
        self.assertEqual(merged["metrics"]["result_count"], 2)
        self.assertEqual(merged["web_search"]["status"], "searched")
        self.assertIs(merged.get("paid_spending", False), False)


if __name__ == "__main__":
    unittest.main()
