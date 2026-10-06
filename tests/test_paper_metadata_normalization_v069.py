"""Paper metadata normalization keeps validation and provenance stable."""

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.autonomous_promotion import _branch_points_for_symbol
from sira.papers import Paper


class PaperMetadataNormalizationTests(unittest.TestCase):
    @staticmethod
    def paper(**overrides):
        fields = {
            "paper_id": " paper-1 ",
            "title": " Example research ",
            "abstract": " Summary ",
            "authors": (" Alice ", "Bob"),
            "year": 2025,
            "doi": " 10.1/example ",
            "url": "https://example.org/paper",
            "open_access_pdf_url": None,
            "retrieved_at": "2025-09-25T00:00:00Z",
            "provider": " Semantic Scholar ",
            "published_date": "2025-09-24",
            "providers": (" Crossref ", "crossref", " Semantic Scholar "),
        }
        fields.update(overrides)
        return Paper(**fields)

    def test_trims_authors_and_keeps_first_case_insensitive_provider_spelling(self):
        paper = self.paper()
        self.assertEqual(paper.authors, ("Alice", "Bob"))
        self.assertEqual(paper.providers, ("Crossref", "Semantic Scholar"))
        self.assertEqual(paper.provider, "Semantic Scholar")
        self.assertEqual(paper.paper_id, "paper-1")
        self.assertEqual(paper.doi, "10.1/example")

        default_providers = self.paper(providers=())
        self.assertEqual(default_providers.providers, ("Semantic Scholar",))

    def test_invalid_author_and_provider_lists_keep_validation_errors(self):
        cases = (
            ({"authors": ["Alice"]}, "authors must be a bounded tuple"),
            ({"authors": (" ",)}, "author names must be nonempty bounded strings"),
            ({"providers": ["Crossref"]}, "providers must be a bounded tuple"),
            ({"providers": (" ",)}, "providers must contain nonempty bounded text"),
            ({"published_date": "2025-02-30"}, "published_date must be YYYY-MM-DD or null"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                self.paper(**changes)

    def test_constructor_meets_local_branch_goal(self):
        self.assertLessEqual(
            _branch_points_for_symbol(ROOT, "src/sira/papers.py", "__post_init__"),
            41,  # This opportunity starts at 46 branches; the goal removes five.
        )


if __name__ == "__main__":
    unittest.main()
