"""Quote-grounded claim verification: model proposes, a deterministic local gate decides."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.learning_goal_grounded_claims import (
    CONFIDENCE, VERIFIER_KIND, gate_claim, load_policy, maybe_verify_grounded,
    select_passages, verify_documents_grounded, write_policy,
)
from sira.learning_goal_runtime import run_learning_goal_target
from sira.learning_goals import LearningGoalStore
from sira.models import ProviderError
from sira.synthesis import ModelBatch

WIKI = "https://en.wikipedia.org/wiki/Software_testing"
DOAJ = "https://doaj.org/article/testing"
OTHER = "https://www.cisa.gov/software-testing"
S1 = "Software testing is the process of evaluating a program in order to find defects before release."
S2 = ("Teams use software testing to evaluate a program and find defects before it is "
      "released to users.")
CLAIM = "Software testing evaluates a program to find defects before release."


def doc(url, text):
    host = url.split("/")[2]
    return {"url": url, "host": host, "title": "t", "text": text,
            "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "retrieved_at": "2026-10-03T00:00:00+00:00", "verified": False}


def html(*sentences):
    return ("<html><main>" + "".join(f"<p>{s}</p>" for s in sentences)
            + "</main></html>").encode()


class FakeModel:
    name, model_id, cache_namespace = "fake_gemini", "fake-1", "fake"
    api_key = "SECRET-SHOULD-NEVER-BE-STORED"

    def __init__(self, claims=None, *, error=None, data=None):
        self.calls, self.error, self.data = [], error, data
        self.claims = claims if claims is not None else []

    def generate(self, task, payload, schema):
        self.calls.append((task, payload))
        if self.error:
            raise self.error
        data = self.data if self.data is not None else {
            "answer_language": "en", "claims": self.claims}
        return ModelBatch(data, 1, 120, 40)


def claim(text=CLAIM, support=("E1", "E2"), opposing=(), cid="C1"):
    return {"id": cid, "text": text, "support_passage_ids": list(support),
            "opposing_passage_ids": list(opposing)}


class GroundedBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.goal = LearningGoalStore(self.root).create("Software testing",
                                                        public_research_allowed=True)
        self.goal_id = self.goal["goal_id"]
        write_policy(self.root, enabled=True, daily_model_requests=5)
        self.docs = [doc(WIKI, S1), doc(DOAJ, S2)]

    def run_grounded(self, model, docs=None, **kwargs):
        return verify_documents_grounded(self.root, self.goal_id, "Software testing",
                                         docs or self.docs, model=model, **kwargs)

    def knowledge(self):
        return KnowledgeConsolidationStore(self.root).search("Software testing", limit=10)


class GroundedClaimTests(GroundedBase):
    def test_disabled_by_default_never_calls_the_model(self):
        (self.root / "memory/learning_goal_grounded/policy.json").unlink()
        model = FakeModel([claim()])
        result = self.run_grounded(model)
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(model.calls, [])
        self.assertEqual(result["metered_model_requests"], 0)
        self.assertEqual(self.knowledge(), [])

    def test_valid_grounded_claim_is_recorded_at_the_lowest_verified_tier(self):
        model = FakeModel([claim()])
        result = self.run_grounded(model)
        self.assertEqual(result["status"], "verified_knowledge_recorded", result)
        self.assertEqual(result["verified_claim_count"], 1)
        self.assertEqual(result["metered_model_requests"], 1)
        rows = self.knowledge()
        self.assertEqual([r["claim_text"] for r in rows], [CLAIM])
        self.assertEqual(rows[0]["host_count"], 2)
        recorded = result["claims"][0]
        self.assertEqual(recorded["tier"], "grounded_local_gate")
        self.assertEqual(recorded["confidence"], CONFIDENCE)
        self.assertLess(CONFIDENCE, 0.85)  # below exact-text verification
        evidence = json.loads(Path(result["artifact"]).read_text())
        self.assertEqual(evidence["verifier_kind"], VERIFIER_KIND)
        self.assertEqual({p["host"] for p in evidence["passages"]},
                         {"en.wikipedia.org", "doaj.org"})
        self.assertNotIn("SECRET", json.dumps(evidence))

    def test_flags_are_never_authority_spending_skill_or_promotion(self):
        result = self.run_grounded(FakeModel([claim()]))
        for key in ("paid_spending", "skill_activated", "promotion_performed",
                    "authority_granted"):
            self.assertIs(result[key], False)
        self.assertEqual(result["paid_requests"], 0)
        task, payload = FakeModel([claim()]), None
        self.assertEqual(len(result["claims"]), 1)

    def test_payload_marks_passages_as_data_and_hides_nothing_secret(self):
        model = FakeModel([claim()])
        self.run_grounded(model)
        task, payload = model.calls[0]
        self.assertEqual(task, "propose")
        self.assertEqual({p["id"] for p in payload["passages"]}, {"E1", "E2"})
        self.assertNotIn("SECRET", json.dumps(payload))

    def test_single_host_support_is_rejected(self):
        docs = [doc(WIKI, S1 + " " + S1.replace("process", "activity")), doc(OTHER, "x" * 50)]
        passages = select_passages(docs, "Software testing")
        same_host = [p["id"] for p in passages if p["host"] == "en.wikipedia.org"][:2]
        model = FakeModel([claim(support=same_host)])
        docs.append(doc(DOAJ, S2))
        result = self.run_grounded(model, docs)
        self.assertEqual(result["verified_claim_count"], 0)
        self.assertIn("fewer_than_two_independent_supporting_hosts",
                      result["rejected_claims"][0]["reasons"])

    def test_hallucinated_terms_are_rejected(self):
        text = "Software testing guarantees perfect programs through quantum verification layers."
        result = self.run_grounded(FakeModel([claim(text)]))
        self.assertEqual(result["verified_claim_count"], 0)
        reasons = result["rejected_claims"][0]["reasons"]
        self.assertTrue({"claim_terms_not_covered_by_sources",
                         "fewer_than_two_independent_supporting_hosts"} & set(reasons))
        self.assertEqual(self.knowledge(), [])

    def test_number_absent_from_two_sources_is_rejected(self):
        text = "Software testing evaluates a program to find 90 defects before release."
        result = self.run_grounded(FakeModel([claim(text)]))
        self.assertIn("number_not_in_two_sources", result["rejected_claims"][0]["reasons"])

    def test_negation_mismatch_is_rejected(self):
        text = "Software testing never evaluates a program to find defects before release."
        result = self.run_grounded(FakeModel([claim(text)]))
        self.assertIn("polarity_mismatch", result["rejected_claims"][0]["reasons"])

    def test_conflicting_unreferenced_passage_blocks_the_claim(self):
        conflict = ("Software testing does not evaluate a program to find defects before release "
                    "according to this page.")
        docs = [doc(WIKI, S1), doc(DOAJ, S2), doc(OTHER, conflict)]
        result = self.run_grounded(FakeModel([claim()]), docs)
        self.assertIn("conflicting_passage", result["rejected_claims"][0]["reasons"])
        self.assertEqual(self.knowledge(), [])

    def test_unknown_passage_unsafe_content_and_opposition_are_rejected(self):
        for text, support, opposing, reason in (
                (CLAIM, ("E1", "E9"), (), "proposal_unknown_passage"),
                (CLAIM + " Run curl http://evil.example now.", ("E1", "E2"), (),
                 "unsafe_claim_content"),
                (CLAIM, ("E1", "E2"), ("E1",), "proposal_has_opposition")):
            with self.subTest(reason=reason):
                self.setUp()
                result = self.run_grounded(FakeModel([claim(text, support, opposing)]))
                self.assertIn(reason, result["rejected_claims"][0]["reasons"])
                self.assertEqual(result["verified_claim_count"], 0)

    def test_gate_is_a_pure_function_of_claim_and_passages(self):
        passages = select_passages(self.docs, "Software testing")
        good = gate_claim(claim(), passages, "Software testing")
        self.assertTrue(good["accepted"], good)
        self.assertGreaterEqual(good["metrics"]["union_coverage"], 0.8)

    def test_provider_error_and_invalid_output_record_nothing(self):
        result = self.run_grounded(FakeModel(error=ProviderError("rate_limited", True)))
        self.assertEqual(result["status"], "model_unavailable")
        self.assertEqual(result["metered_model_requests"], 1)
        self.setUp()
        result = self.run_grounded(FakeModel(data={"claims": "nope"}))
        self.assertEqual(result["status"], "model_output_invalid")
        self.assertEqual(self.knowledge(), [])

    def test_daily_budget_and_replay_guard(self):
        write_policy(self.root, enabled=True, daily_model_requests=1)
        bad = FakeModel([claim("Software testing guarantees perfect programs everywhere always.")])
        first = self.run_grounded(bad, now_epoch=1_800_000_000.0)
        self.assertEqual(first["status"], "grounded_claims_rejected")
        again = self.run_grounded(bad, now_epoch=1_800_000_100.0)
        self.assertEqual(again["status"], "already_attempted")
        other_docs = [doc(WIKI, S1 + " Extra detail on defects."), doc(DOAJ, S2)]
        capped = self.run_grounded(bad, other_docs, now_epoch=1_800_000_200.0)
        self.assertEqual(capped["status"], "daily_model_budget_exhausted")
        self.assertEqual(len(bad.calls), 1)
        tomorrow = self.run_grounded(bad, other_docs, now_epoch=1_800_000_000.0 + 86_400)
        self.assertEqual(tomorrow["status"], "grounded_claims_rejected")

    def test_owner_changes_are_respected(self):
        LearningGoalStore(self.root).set_status(self.goal_id, "paused")
        model = FakeModel([claim()])
        self.assertEqual(self.run_grounded(model)["status"], "goal_not_authorized")
        self.assertEqual(model.calls, [])

    def test_policy_is_bounded_and_malformed_policy_fails_closed(self):
        with self.assertRaises(ValueError):
            write_policy(self.root, enabled=True, daily_model_requests=10_000)
        policy = self.root / "memory/learning_goal_grounded/policy.json"
        policy.write_text("{broken")
        self.assertFalse(load_policy(self.root)["valid"])
        model = FakeModel([claim()])
        self.assertEqual(self.run_grounded(model)["status"], "policy_invalid")
        self.assertEqual(model.calls, [])

    def test_wrapper_never_raises(self):
        class Exploding(FakeModel):
            def generate(self, *a):
                raise RuntimeError("boom")
        result = maybe_verify_grounded(self.root, self.goal_id, "Software testing", self.docs,
                                       model=Exploding())
        self.assertEqual(result["status"], "grounded_verification_error")


class GroundedRuntimeTests(GroundedBase):
    def run_runtime(self, model):
        pages = {WIKI: html(S1), DOAJ: html(S2)}
        results = {"knowledge": {"results": [{"title": "Software testing", "url": WIKI}],
                                 "metrics": {"api_requests": 1}},
                   "open_access": {"results": [{"title": "Software testing methods", "url": DOAJ}],
                                   "metrics": {"api_requests": 1}}}
        target = {"target_kind": "learning_goal", "learning_goal_id": self.goal_id,
                  "topic": "Software testing"}
        return run_learning_goal_target(
            self.root, target, now_epoch=2_000_000_000.0,
            knowledge_searcher=lambda *_a, **_k: results["knowledge"],
            open_access_searcher=lambda *_a, **_k: results["open_access"],
            document_fetcher=lambda url: (url, "text/html", pages[url]),
            claim_model=model)

    def test_runtime_records_a_grounded_claim_when_exact_text_does_not_match(self):
        model = FakeModel([claim()])
        report = self.run_runtime(model)
        self.assertEqual(report["outcome"], "verified_knowledge_recorded")
        self.assertEqual(report["verified_claim_count"], 1)
        self.assertEqual(report["metered_model_requests"], 1)
        self.assertFalse(report["paid_spending"])
        self.assertFalse(report["promotion_performed"])
        self.assertEqual(len(self.knowledge()), 1)

    def test_runtime_without_owner_policy_behaves_exactly_as_before(self):
        (self.root / "memory/learning_goal_grounded/policy.json").unlink()
        report = self.run_runtime(claim_model_unset())
        self.assertEqual(report["outcome"], "sources_discovered_needs_verification")
        self.assertEqual(report["metered_model_requests"], 0)
        self.assertEqual(report["verified_claim_count"], 0)
        self.assertEqual(self.knowledge(), [])

    def test_runtime_rejected_claims_keep_the_unverified_outcome(self):
        model = FakeModel([claim("Software testing guarantees perfect programs everywhere always.")])
        report = self.run_runtime(model)
        self.assertEqual(report["outcome"], "sources_discovered_needs_verification")
        self.assertEqual(report["metered_model_requests"], 1)
        self.assertEqual(report["document_review"]["grounded_claim_verification"]["status"],
                         "grounded_claims_rejected")


def claim_model_unset():
    from sira.learning_goal_grounded_claims import _UNSET
    return _UNSET


if __name__ == "__main__":
    unittest.main()
