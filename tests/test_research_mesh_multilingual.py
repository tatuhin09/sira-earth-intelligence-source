from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [x for x in sys.path if x != str(SRC)]

from sira.papers import PaperBatch
from sira.providers.gemini_language import LanguageModelBatch
from sira.research_mesh import prepare_multilingual_research_query, search_biomedical_free


class FakeLanguageModel:
    def __init__(self):
        self.calls = 0

    def interpret(self, text, profile):
        self.calls += 1
        return LanguageModelBatch({
            "detected_language": "Banglish",
            "canonical_english": "heart disease treatment evidence",
            "research_queries": ["heart disease treatment clinical evidence"],
            "candidate_mappings": [],
            "uncertainty": "low",
        }, 1, 12, 6)

    def verify(self, text, proposal):
        self.calls += 1
        return LanguageModelBatch({
            "accepted": True,
            "semantic_equivalent": True,
            "intent_preserved": True,
            "verified_canonical_english": "heart disease treatment evidence",
            "verified_research_queries": ["heart disease treatment clinical evidence"],
            "mapping_verdicts": [],
            "reason": "verified",
        }, 1, 8, 4)


class FakeBiomedical:
    name = "fake_biomedical"
    cache_namespace = "fake-biomedical-v1"

    def __init__(self):
        self.queries = []

    def search(self, query, max_results):
        self.queries.append(query)
        return PaperBatch((), api_requests=0)


class ResearchMeshMultilingualTests(unittest.TestCase):
    def test_english_preparation_is_local_and_model_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = FakeLanguageModel()
            row = prepare_multilingual_research_query(
                Path(tmp),
                'Please research heart disease treatment evidence',
                allow_model=True,
                environ={
                    "GEMINI_API_KEY": "configured",
                    "SIRA_GEMINI_FREE_TIER_CONFIRMED": "true",
                },
                model=model,
            )
        self.assertEqual(row["mode"], "local_original")
        self.assertEqual(model.calls, 0)

    def test_banglish_verified_query_is_used_by_biomedical_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = FakeLanguageModel()
            provider = FakeBiomedical()
            original = "ami heart disease er treatment evidence chai amk bolo"
            report = search_biomedical_free(
                root,
                original,
                providers=(provider,),
                use_cache=False,
                allow_language_model=True,
                language_environ={
                    "GEMINI_API_KEY": "configured",
                    "SIRA_GEMINI_FREE_TIER_CONFIRMED": "true",
                },
                language_model=model,
            )
        self.assertEqual(provider.queries, ["heart disease treatment clinical evidence"])
        self.assertEqual(report["original_query"], original)
        self.assertEqual(report["query_preparation"]["mode"], "verified_semantic_bridge")
        self.assertFalse(report["paid_spending"])

    def test_blocked_bridge_falls_back_without_claiming_verified_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = FakeLanguageModel()
            row = prepare_multilingual_research_query(
                Path(tmp),
                "ami heart disease er treatment evidence chai amk bolo",
                allow_model=True,
                environ={"GEMINI_API_KEY": "configured"},
                model=model,
            )
        self.assertIn(row["mode"], {"local_original", "local_learned_gloss"})
        self.assertEqual(model.calls, 0)
        self.assertEqual(row["semantic_bridge_status"], "blocked")
        self.assertFalse(row["translation_verified"])


if __name__ == "__main__":
    unittest.main()
