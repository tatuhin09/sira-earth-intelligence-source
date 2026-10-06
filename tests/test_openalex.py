import unittest
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class OpenAlexProviderTests(unittest.TestCase):
    def test_search_is_bounded_anonymous_and_reconstructs_abstract(self):
        from sira.providers.openalex import OpenAlexProvider

        payload = {
            "results": [
                {
                    "id": "https://openalex.org/W123",
                    "title": "Refactoring Complex Functions",
                    "doi": "https://doi.org/10.1000/example",
                    "publication_year": 2025,
                    "publication_date": "2025-03-04",
                    "authorships": [
                        {"author": {"display_name": "Ada Researcher"}},
                        {"author": {"display_name": "Grace Engineer"}},
                    ],
                    "abstract_inverted_index": {
                        "Behavior": [0],
                        "preserving": [1],
                        "refactoring": [2],
                        "reduces": [3],
                        "complexity": [4],
                    },
                    "best_oa_location": {"pdf_url": "https://example.org/paper.pdf"},
                }
            ]
        }

        captured = {}

        def fake_request_json(request, timeout, opener):
            captured["request"] = request
            captured["timeout"] = timeout
            return payload

        with patch("sira.providers.openalex.request_json", side_effect=fake_request_json):
            batch = OpenAlexProvider(timeout=17).search("refactoring complex function", 3)

        self.assertEqual(batch.api_requests, 1)
        self.assertEqual(batch.providers_attempted, ("openalex",))
        self.assertEqual(len(batch.papers), 1)
        paper = batch.papers[0]
        self.assertEqual(paper.paper_id, "openalex:W123")
        self.assertEqual(paper.doi, "10.1000/example")
        self.assertEqual(paper.abstract, "Behavior preserving refactoring reduces complexity")
        self.assertEqual(paper.authors, ("Ada Researcher", "Grace Engineer"))
        self.assertEqual(paper.year, 2025)
        self.assertEqual(paper.published_date, "2025-03-04")
        self.assertEqual(paper.provider, "openalex")
        self.assertEqual(paper.open_access_pdf_url, "https://example.org/paper.pdf")

        request = captured["request"]
        parts = urlsplit(request.full_url)
        self.assertEqual(parts.scheme, "https")
        self.assertEqual(parts.netloc, "api.openalex.org")
        self.assertEqual(parts.path, "/works")
        query = parse_qs(parts.query)
        self.assertEqual(query["search"], ["refactoring complex function"])
        self.assertEqual(query["per_page"], ["3"])
        self.assertNotIn("api_key", query)
        self.assertEqual(captured["timeout"], 17)

    def test_bad_rows_are_rejected_without_poisoning_valid_rows(self):
        from sira.providers.openalex import OpenAlexProvider

        payload = {
            "results": [
                {"id": "not-an-openalex-id", "title": "Bad"},
                {
                    "id": "https://openalex.org/W999",
                    "title": "Valid Work",
                    "doi": None,
                    "publication_year": 2024,
                    "publication_date": "2024-01-01",
                    "authorships": [],
                    "abstract_inverted_index": None,
                    "primary_location": {"landing_page_url": "https://openalex.org/W999"},
                },
            ]
        }
        with patch("sira.providers.openalex.request_json", return_value=payload):
            batch = OpenAlexProvider().search("maintainability", 3)

        self.assertEqual(len(batch.papers), 1)
        self.assertEqual(batch.rejected_papers, 1)
        self.assertEqual(batch.papers[0].paper_id, "openalex:W999")
        self.assertIsNone(batch.papers[0].abstract)


if __name__ == "__main__":
    unittest.main()
