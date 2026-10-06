from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [x for x in sys.path if x != str(SRC)]

from sira.access_requests import AccessRequestStore
from sira.language_semantic_bridge import bridge_multilingual_query
from sira.learning_consolidation import LearningConsolidationStore
from sira.provider_catalog import get_provider
from sira.providers.gemini_language import LanguageModelBatch


class FakeModel:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.calls = 0

    def interpret(self, text, profile):
        self.calls += 1
        return LanguageModelBatch({
            "detected_language": "Banglish",
            "canonical_english": "I will do it now.",
            "research_queries": ["perform current task"],
            "candidate_mappings": [
                {"token": "korbo", "meaning": "will do", "confidence": 0.95},
                {"token": "invented", "meaning": "bad", "confidence": 0.99},
            ],
            "uncertainty": "low",
        }, 1, 10, 5)

    def verify(self, text, proposal):
        self.calls += 1
        return LanguageModelBatch({
            "accepted": self.accepted,
            "semantic_equivalent": self.accepted,
            "intent_preserved": self.accepted,
            "verified_canonical_english": "I will do it now.",
            "verified_research_queries": ["perform current task"],
            "mapping_verdicts": [
                {"token": "korbo", "meaning": "will do", "supported": self.accepted, "confidence": 0.94},
                {"token": "invented", "meaning": "bad", "supported": True, "confidence": 0.99},
            ],
            "reason": "verified" if self.accepted else "rejected",
        }, 1, 8, 4)


class LanguageSemanticBridgeTests(unittest.TestCase):
    def test_catalog_formally_exposes_language_semantic_bridge(self):
        self.assertIn("language_semantic_bridge", get_provider("gemini").capabilities)

    def test_local_english_never_calls_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = FakeModel()
            row = bridge_multilingual_query(
                Path(tmp),
                "Please test this code and report the result",
                allow_model=True,
                environ={
                    "SIRA_GEMINI_FREE_TIER_CONFIRMED": "true",
                    "GEMINI_API_KEY": "configured",
                },
                model=model,
            )
        self.assertEqual(row["status"], "local_only")
        self.assertEqual(model.calls, 0)

    def test_missing_zero_cost_confirmation_blocks_before_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = FakeModel()
            row = bridge_multilingual_query(
                Path(tmp),
                "ami akhon code korbo amk bolo",
                allow_model=True,
                environ={"GEMINI_API_KEY": "configured"},
                model=model,
            )
        self.assertEqual(row["status"], "blocked")
        self.assertTrue(row["owner_action_required"])
        self.assertEqual(model.calls, 0)

    def test_missing_key_creates_metadata_only_access_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = bridge_multilingual_query(
                root,
                "ami akhon code korbo amk bolo",
                allow_model=True,
                environ={"SIRA_GEMINI_FREE_TIER_CONFIRMED": "true"},
                model=FakeModel(),
            )
            notices = AccessRequestStore(root).pending_notifications()
        self.assertEqual(row["status"], "blocked")
        self.assertIsInstance(row["access_request_id"], str)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["credential_name"], "GEMINI_API_KEY")
        self.assertFalse(notices[0]["contains_secret_value"])

    def test_verified_mapping_needs_two_distinct_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "SIRA_GEMINI_FREE_TIER_CONFIRMED": "true",
                "GEMINI_API_KEY": "configured",
            }
            first = bridge_multilingual_query(
                root,
                "ami akhon korbo amk bolo",
                allow_model=True,
                environ=env,
                use_cache=False,
                model=FakeModel(),
            )
            store = LearningConsolidationStore(root)
            self.assertEqual(first["status"], "completed")
            self.assertEqual(first["mapping_evidence_recorded"], 1)
            self.assertNotIn("korbo", store.active_lexicon())

            second = bridge_multilingual_query(
                root,
                "ami pore korbo amk bolo",
                allow_model=True,
                environ=env,
                use_cache=False,
                model=FakeModel(),
            )
            self.assertEqual(second["status"], "completed")
            self.assertEqual(store.active_lexicon()["korbo"], "will do")

    def test_hallucinated_mapping_not_in_original_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = bridge_multilingual_query(
                root,
                "ami akhon korbo",
                allow_model=True,
                environ={
                    "SIRA_GEMINI_FREE_TIER_CONFIRMED": "true",
                    "GEMINI_API_KEY": "configured",
                },
                use_cache=False,
                model=FakeModel(),
            )
            stats = LearningConsolidationStore(root).stats()
        self.assertEqual(row["mapping_evidence_recorded"], 1)
        self.assertEqual(stats["language_mapping_evidence"], 1)

    def test_verifier_rejection_cannot_enter_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = bridge_multilingual_query(
                root,
                "ami akhon korbo",
                allow_model=True,
                environ={
                    "SIRA_GEMINI_FREE_TIER_CONFIRMED": "true",
                    "GEMINI_API_KEY": "configured",
                },
                use_cache=False,
                model=FakeModel(accepted=False),
            )
            stats = LearningConsolidationStore(root).stats()
        self.assertEqual(row["status"], "verification_rejected")
        self.assertEqual(row["mapping_evidence_recorded"], 0)
        self.assertEqual(stats["language_mapping_evidence"], 0)

    def test_cache_does_not_forge_second_learning_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "SIRA_GEMINI_FREE_TIER_CONFIRMED": "true",
                "GEMINI_API_KEY": "configured",
            }
            model = FakeModel()
            first = bridge_multilingual_query(
                root, "ami akhon korbo",
                allow_model=True, environ=env, model=model,
            )
            calls = model.calls
            second = bridge_multilingual_query(
                root, "ami akhon korbo",
                allow_model=True, environ=env, model=model,
            )
            stats = LearningConsolidationStore(root).stats()
        self.assertEqual(first["status"], "completed")
        self.assertTrue(second["metrics"]["cache_hit"])
        self.assertEqual(model.calls, calls)
        self.assertEqual(second["mapping_evidence_recorded"], 0)
        self.assertEqual(stats["language_mapping_evidence"], 1)


if __name__ == "__main__":
    unittest.main()
