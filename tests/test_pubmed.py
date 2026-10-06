from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [item for item in sys.path if item != str(SRC)]

from sira.providers.pubmed import PubMedProvider


class PubMedProviderTests(unittest.TestCase):
    def test_search_uses_bounded_esearch_then_esummary(self):
        responses = [
            {"esearchresult": {"idlist": ["12345"]}},
            {
                "result": {
                    "uids": ["12345"],
                    "12345": {
                        "title": "PubMed study",
                        "pubdate": "2026 Jan",
                        "authors": [{"name": "Researcher A"}],
                        "articleids": [{"idtype": "doi", "value": "10.1000/pubmed"}],
                    },
                }
            },
        ]
        sleeps = []
        with patch("sira.providers.pubmed.request_json", side_effect=responses) as mocked:
            provider = PubMedProvider(
                api_key="SECRET-KEY",
                contact_email="owner@example.org",
                clock=lambda: 100.0,
                sleeper=lambda value: sleeps.append(value),
            )
            batch = provider.search("biomedical AI", 3)

        self.assertEqual(batch.api_requests, 2)
        self.assertEqual(len(batch.papers), 1)
        paper = batch.papers[0]
        self.assertEqual(paper.paper_id, "pubmed:12345")
        self.assertEqual(paper.doi, "10.1000/pubmed")
        self.assertEqual(paper.year, 2026)

        first_request = mocked.call_args_list[0].args[0]
        first_query = parse_qs(urlsplit(first_request.full_url).query)
        self.assertEqual(first_query["retmax"], ["3"])
        self.assertEqual(first_query["api_key"], ["SECRET-KEY"])
        self.assertEqual(first_query["email"], ["owner@example.org"])
        self.assertTrue(sleeps)

    def test_empty_id_list_uses_one_request(self):
        with patch(
            "sira.providers.pubmed.request_json",
            return_value={"esearchresult": {"idlist": []}},
        ):
            batch = PubMedProvider(
                clock=lambda: 100.0,
                sleeper=lambda _value: None,
            ).search("rare query", 2)
        self.assertEqual(batch.api_requests, 1)
        self.assertEqual(batch.papers, ())


if __name__ == "__main__":
    unittest.main()
