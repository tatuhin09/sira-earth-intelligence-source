"""Document reading v2: safe redirects, readable leads, refusing-host memory, ranking."""
from __future__ import annotations

from email.message import Message
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira import learning_goal_general_learning as reading
from sira.learning_goal_general_engine import _candidates
from sira.learning_goal_grounded_claims import gate_claim, select_passages
from sira.learning_goal_runtime import _with_web_discovery
from sira.learning_goals import LearningGoalStore
from sira.models import Source
from sira.source_fetch_health import (
    BLOCK_AFTER_FAILURES, BLOCK_SECONDS, blocked_hosts, drop_blocked_rows, observe_fetches,
)
from sira.trusted_hosts import add_hosts
from sira.web_search_discovery import normalize_lead_url, search_trusted_web, write_policy

NOW = 1_790_000_000.0


class Response:
    def __init__(self, url, body=b"<html><p>ok</p></html>", ctype="text/html"):
        self._url, self._body = url, body
        self.headers = Message()
        self.headers["Content-Type"] = ctype

    def geturl(self):
        return self._url

    def read(self, limit):
        return self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def redirect(url, code, location):
    headers = Message()
    headers["Location"] = location
    return HTTPError(url, code, "moved", headers, io.BytesIO(b""))


def fake_open(routes):
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        result = routes[request.full_url]
        if isinstance(result, Exception):
            raise result
        return result
    opener.calls = calls
    return opener


class RedirectTests(unittest.TestCase):
    def fetch(self, routes, url="https://www.w3.org/2018/10/credibility-tech"):
        opener = fake_open(routes)
        with patch.object(reading, "open_request", opener):
            return reading._fetch_public_document(url), opener.calls

    def test_same_host_https_redirect_is_followed_and_reports_the_requested_url(self):
        start = "https://www.w3.org/2018/10/credibility-tech"
        final = start + "/"
        (url, ctype, body), calls = self.fetch(
            {start: redirect(start, 301, "/2018/10/credibility-tech/"), final: Response(final)})
        self.assertEqual((url, ctype), (start, "text/html"))
        self.assertEqual(calls, [start, final])

    def test_direct_success_is_unchanged(self):
        start = "https://docs.python.org/3/library/unittest.html"
        (url, ctype, _body), calls = self.fetch({start: Response(start)}, start)
        self.assertEqual((url, calls), (start, [start]))

    def test_unsafe_redirects_are_refused(self):
        start = "https://www.w3.org/a"
        for location in ("https://evil.example.org/a", "http://www.w3.org/a/",
                         "https://user:pw@www.w3.org/a/", "https://www.w3.org:8443/a/",
                         "//evil.example.org/a", "https://www.w3.org/a/#frag"):
            with self.subTest(location=location):
                routes = {start: redirect(start, 301, location)}
                with self.assertRaises(HTTPError) as caught:
                    self.fetch(routes, start)
                self.assertEqual(caught.exception.code, 301)

    def test_redirect_chain_is_bounded(self):
        a, b, c, d = (f"https://www.w3.org/{n}" for n in "abcd")
        routes = {a: redirect(a, 302, b), b: redirect(b, 302, c), c: redirect(c, 302, d),
                  d: Response(d)}
        with self.assertRaises(HTTPError):
            self.fetch(routes, a)
        routes = {a: redirect(a, 302, b), b: redirect(b, 302, c), c: Response(c)}
        (url, _ctype, _body), calls = self.fetch(routes, a)
        self.assertEqual((url, len(calls)), (a, 3))

    def test_non_redirect_errors_and_missing_location_propagate(self):
        start = "https://doaj.org/article/1"
        with self.assertRaises(HTTPError) as caught:
            self.fetch({start: HTTPError(start, 403, "no", Message(), io.BytesIO(b""))}, start)
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(HTTPError):
            self.fetch({start: HTTPError(start, 301, "moved", Message(), io.BytesIO(b""))}, start)


class LeadNormalizationTests(unittest.TestCase):
    def test_arxiv_variants_become_the_abstract_page(self):
        for url in ("https://arxiv.org/html/2510.13749", "https://arxiv.org/pdf/2510.13749v2",
                    "https://arxiv.org/pdf/2510.13749v2.pdf", "https://arxiv.org/abs/2510.13749v3"):
            with self.subTest(url=url):
                self.assertEqual(normalize_lead_url(url), "https://arxiv.org/abs/2510.13749")
        self.assertEqual(normalize_lead_url("https://arxiv.org/pdf/hep-th/9901001"),
                         "https://arxiv.org/abs/hep-th/9901001")
        self.assertIsNone(normalize_lead_url("https://arxiv.org/list/cs.AI/recent"))

    def test_pdf_documents_are_kept_but_other_downloads_and_insecure_urls_are_dropped(self):
        pdf_url = "https://www.cisa.gov/sites/default/files/2026-08/guide.pdf"
        self.assertEqual(normalize_lead_url(pdf_url), pdf_url)
        for url in ("https://example.org/data.CSV", "http://docs.python.org/3/", "ftp://x.org/a"):
            with self.subTest(url=url):
                self.assertIsNone(normalize_lead_url(url))
        self.assertEqual(normalize_lead_url("https://docs.python.org/3/library/unittest.html"),
                         "https://docs.python.org/3/library/unittest.html")

    def test_search_returns_readable_deduplicated_leads_and_counts_drops(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_policy(root, enabled=True)
            add_hosts(root, ["docs.python.org"])

            def searcher(_root, _query, _max, _domains):
                return [Source("A paper", "https://arxiv.org/html/2510.13749", "x", "2026-10-01"),
                        Source("A paper", "https://arxiv.org/pdf/2510.13749", "x", "2026-10-01"),
                        Source("CISA guide", "https://www.cisa.gov/files/guide.pdf", "x", "2026-10-01"),
                        Source("Docs", "https://docs.python.org/3/library/unittest.html", "x",
                               "2026-10-01")], 1
            result = search_trusted_web(root, "unit testing frameworks", now_epoch=NOW,
                                        searcher=searcher)
        self.assertEqual([r["url"] for r in result["rows"]],
                         ["https://arxiv.org/abs/2510.13749",
                          "https://www.cisa.gov/files/guide.pdf",
                          "https://docs.python.org/3/library/unittest.html"])
        self.assertEqual(result["leads_dropped"], 0)


class FetchHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def refuse(self, host="doaj.org", times=1, at=NOW, code="http_403"):
        observe_fetches(self.root, failures=[{"host": host, "url": "https://x", "code": code}] * times,
                        now_epoch=at)

    def test_repeated_refusals_block_the_host_until_it_expires(self):
        self.refuse(times=BLOCK_AFTER_FAILURES - 1)
        self.assertEqual(blocked_hosts(self.root, NOW), frozenset())
        self.refuse()
        self.assertEqual(blocked_hosts(self.root, NOW), frozenset({"doaj.org"}))
        self.assertEqual(blocked_hosts(self.root, NOW + BLOCK_SECONDS + 1), frozenset())

    def test_only_refusal_codes_count_and_success_clears(self):
        for code in ("http_301", "invalid_source_content", "network_or_timeout", "http_500"):
            self.refuse(host="www.w3.org", times=5, code=code)
        self.assertEqual(blocked_hosts(self.root, NOW), frozenset())
        self.refuse(times=BLOCK_AFTER_FAILURES)
        self.assertIn("doaj.org", blocked_hosts(self.root, NOW))
        observe_fetches(self.root, ok_hosts=["doaj.org"], now_epoch=NOW + 5)
        self.assertEqual(blocked_hosts(self.root, NOW + 5), frozenset())

    def test_rows_from_refusing_hosts_are_dropped_and_counted(self):
        self.refuse(times=BLOCK_AFTER_FAILURES)
        report = {"results": [{"url": "https://doaj.org/article/1"},
                              {"url": "https://arxiv.org/abs/1"}],
                  "metrics": {"api_requests": 2, "result_count": 2}}
        result = drop_blocked_rows(self.root, report, NOW)
        self.assertEqual([r["url"] for r in result["results"]], ["https://arxiv.org/abs/1"])
        self.assertEqual((result["rows_skipped_refusing_hosts"], result["metrics"]["result_count"]),
                         (1, 1))
        self.assertEqual(len(report["results"]), 2)

    def test_default_search_path_drops_refusing_hosts_even_when_web_search_is_off(self):
        self.refuse(times=BLOCK_AFTER_FAILURES, at=NOW)
        base = {"results": [{"url": "https://doaj.org/article/1"}, {"url": "https://arxiv.org/abs/1"}],
                "metrics": {"api_requests": 2, "result_count": 2}}
        merged = _with_web_discovery(self.root, "software testing", dict(base), NOW + 10)
        self.assertEqual([r["url"] for r in merged["results"]], ["https://arxiv.org/abs/1"])

    def test_malformed_or_hostile_state_blocks_nothing_and_never_raises(self):
        path = self.root / "memory/source_fetch_health/health.json"
        path.parent.mkdir(parents=True)
        for text in ("{bad", json.dumps({"schema": "x"}), json.dumps({"schema": "sira.source_fetch_health.v1",
                                                                     "hosts": {"a.org": {"refusals": "9"}}})):
            path.write_text(text)
            self.assertEqual(blocked_hosts(self.root, NOW), frozenset())
            observe_fetches(self.root, failures=[None, {"host": 5}], now_epoch=NOW)

    def test_the_host_list_is_bounded(self):
        for index in range(150):
            self.refuse(host=f"h{index}.example.org", at=NOW + index)
        stored = json.loads((self.root / "memory/source_fetch_health/health.json").read_text())
        self.assertLessEqual(len(stored["hosts"]), 100)


class RankingAndPassageTests(unittest.TestCase):
    def test_owner_trusted_documentation_outranks_scholarly_hits(self):
        research = {"knowledge": {"results": [
            {"title": "Pytest fixtures guide", "url": "https://en.wikipedia.org/wiki/Pytest"}]},
            "open_access": {"results": [
                {"title": "Pytest fixtures guide", "url": "https://arxiv.org/abs/2401.00001"},
                {"title": "Pytest fixtures guide", "url": "https://docs.pytest.org/en/stable/fixture.html"},
                {"title": "Pytest fixtures guide", "url": "https://www.nasa.gov/pytest-fixtures"}]}}
        trusted = frozenset({"docs.pytest.org"})
        order = [row["host"] for row in _candidates("Pytest fixtures guide", research, trusted)]
        self.assertEqual(order[:3], ["www.nasa.gov", "docs.pytest.org", "arxiv.org"])

    def test_loose_passage_selection_keeps_the_gate_strict(self):
        text_a = ("Secure code review examines program source for security flaws before release "
                  "by a second engineer.")
        text_b = ("A review of source code by another engineer can reveal security flaws before "
                  "release to production users.")
        docs = [{"url": "https://a.example.org/x", "host": "a.example.org", "text": text_a,
                 "content_sha256": "1" * 64, "retrieved_at": "2026-10-01T00:00:00+00:00"},
                {"url": "https://b.example.org/y", "host": "b.example.org", "text": text_b,
                 "content_sha256": "2" * 64, "retrieved_at": "2026-10-01T00:00:00+00:00"}]
        focus = "Secure code review and defensive testing"
        passages = select_passages(docs, focus)
        self.assertEqual({p["host"] for p in passages}, {"a.example.org", "b.example.org"})
        off_topic = {"id": "C1", "text": "Bananas contain potassium and grow in warm climates worldwide.",
                     "support_passage_ids": [p["id"] for p in passages], "opposing_passage_ids": []}
        decision = gate_claim(off_topic, passages, focus)
        self.assertFalse(decision["accepted"])
        self.assertTrue({"claim_off_topic", "claim_terms_not_covered_by_sources"} & set(decision["reasons"]))


class RuntimeObservationTests(unittest.TestCase):
    def test_goal_attempt_records_refusals_from_failed_reads(self):
        from sira.learning_goal_runtime import run_learning_goal_target
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            goal = LearningGoalStore(root).create("Software testing", public_research_allowed=True)
            wiki = "https://en.wikipedia.org/wiki/Software_testing"
            doaj = "https://doaj.org/article/x"

            def fetcher(url):
                if "doaj.org" in url:
                    raise HTTPError(url, 403, "forbidden", Message(), io.BytesIO(b""))
                return url, "text/html", b"<html><main><p>Software testing finds defects early.</p></main></html>"
            run_learning_goal_target(
                root, {"target_kind": "learning_goal", "learning_goal_id": goal["goal_id"],
                       "topic": "Software testing"}, now_epoch=NOW,
                knowledge_searcher=lambda *_a, **_k: {"results": [{"title": "Software testing", "url": wiki}],
                                                      "metrics": {"api_requests": 1}},
                open_access_searcher=lambda *_a, **_k: {"results": [{"title": "Software testing methods", "url": doaj}],
                                                       "metrics": {"api_requests": 1}},
                document_fetcher=fetcher)
            stored = json.loads((root / "memory/source_fetch_health/health.json").read_text())
            self.assertEqual(stored["hosts"]["doaj.org"]["refusals"], 1)
            self.assertNotIn("en.wikipedia.org", stored["hosts"])


if __name__ == "__main__":
    unittest.main()
