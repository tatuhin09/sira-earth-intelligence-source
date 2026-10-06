from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [item for item in sys.path if item != str(SRC)]

from sira.providers.europe_pmc import EuropePMCProvider


class EuropePMCProviderTests(unittest.TestCase):
    def test_public_core_search_is_bounded_and_parses_abstract(self):
        payload = {
            "resultList": {
                "result": [{
                    "source": "MED",
                    "id": "12345678",
                    "title": "A biomedical study",
                    "abstractText": "Evidence from a bounded biomedical abstract.",
                    "pubYear": "2026",
                    "doi": "10.1000/example",
                    "firstPublicationDate": "2026-01-02",
                    "authorList": {"author": [{"fullName": "A Researcher"}]},
                }]
            }
        }
        with patch("sira.providers.europe_pmc.request_json", return_value=payload) as mocked:
            batch = EuropePMCProvider(timeout=10).search("precision medicine", 3)

        self.assertEqual(batch.api_requests, 1)
        self.assertEqual(len(batch.papers), 1)
        paper = batch.papers[0]
        self.assertEqual(paper.provider, "europe_pmc")
        self.assertEqual(paper.abstract, "Evidence from a bounded biomedical abstract.")
        self.assertEqual(paper.doi, "10.1000/example")
        request = mocked.call_args.args[0]
        parsed = urlsplit(request.full_url)
        query = parse_qs(parsed.query)
        self.assertEqual(query["pageSize"], ["3"])
        self.assertEqual(query["resultType"], ["core"])
        self.assertEqual(query["format"], ["json"])
        self.assertNotIn("api_key", query)

    def test_bad_row_is_rejected_without_poisoning_valid_row(self):
        payload = {
            "resultList": {
                "result": [
                    {"source": "MED", "id": "1", "title": "Valid"},
                    {"source": "MED", "id": "../bad", "title": "Invalid"},
                ]
            }
        }
        with patch("sira.providers.europe_pmc.request_json", return_value=payload):
            batch = EuropePMCProvider().search("query", 2)
        self.assertEqual(len(batch.papers), 1)
        self.assertEqual(batch.rejected_papers, 1)


if __name__ == "__main__":
    unittest.main()
