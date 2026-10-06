from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

# SIRA_TEST_SRC_PATH_FIX_V1
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SRC_TEXT = str(SRC)
sys.path[:] = [SRC_TEXT] + [entry for entry in sys.path if entry != SRC_TEXT]

from sira.memory import MemoryStore, Observation
from sira.opportunity_research import _default_providers
from sira.provider_catalog import list_providers
from sira.provider_router_advisory import build_router_advisory


class StrategyRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _record_strategy_success(
        self,
        strategy_id: str,
        *,
        capability: str = "paper_search",
        count: int = 2,
    ) -> None:
        store = MemoryStore(self.root)
        memory_id, _ = store.upsert(
            Observation(
                "success",
                "success",
                capability,
                f"{strategy_id} strategy succeeded",
                "validated",
                provider=strategy_id,
                signature=f"strategy-runtime-{strategy_id}",
            ),
            run_id=(strategy_id[:2] or "aa") * 16,
            artifact_name="result.json",
            artifact_sha256="a" * 64,
            outcome_status="completed",
        )
        for _ in range(count):
            store.record_outcome(
                memory_id,
                "application_succeeded",
                context={
                    "strategy_id": strategy_id,
                    "capability": capability,
                },
                weight=1.0,
            )

    def _ready_health(self, *, blocked_id: str | None = None):
        rows = []
        for descriptor in list_providers(capability="paper_search"):
            blocked = descriptor.provider_id == blocked_id
            rows.append({
                "provider_id": descriptor.provider_id,
                "available_now": not blocked,
                "health": "cooldown" if blocked else "ready",
                "historical_unreliable": False,
                "cooldown_active": blocked,
                "policy_reason": "cooldown_active" if blocked else "ready",
            })
        return {"providers": rows}

    def test_router_no_history_preserves_existing_deterministic_order(self):
        snapshot = self._ready_health()
        first = build_router_advisory(
            self.root,
            capability="paper_search",
            health_snapshot=snapshot,
        )
        second = build_router_advisory(
            self.root,
            capability="paper_search",
            health_snapshot=snapshot,
        )
        self.assertEqual(
            first["recommended_provider_ids"],
            second["recommended_provider_ids"],
        )
        self.assertFalse(first.get("strategy_learning_applied", False))

    def test_router_repeated_success_reorders_only_eligible_same_band_provider(self):
        self._record_strategy_success("openalex", count=2)
        advisory = build_router_advisory(
            self.root,
            capability="paper_search",
            health_snapshot=self._ready_health(),
        )
        self.assertTrue(advisory["strategy_learning_applied"])
        self.assertEqual(advisory["recommended_provider_ids"][0], "openalex")
        row = next(
            item for item in advisory["recommendations"]
            if item["provider_id"] == "openalex"
        )
        self.assertGreater(row["learned_adjustment"], 0.0)
        self.assertGreaterEqual(row["strategy_evidence_events"], 2)

    def test_router_learning_never_resurrects_cooldown_blocked_provider(self):
        self._record_strategy_success("openalex", count=4)
        advisory = build_router_advisory(
            self.root,
            capability="paper_search",
            health_snapshot=self._ready_health(blocked_id="openalex"),
        )
        self.assertNotIn("openalex", advisory["recommended_provider_ids"])
        self.assertIn("openalex", advisory["blocked_provider_ids"])

    def test_opportunity_provider_order_is_unchanged_without_learning(self):
        names = [provider.name for provider in _default_providers(self.root)]
        self.assertEqual(names, ["arxiv", "crossref", "openalex"])

    def test_opportunity_provider_order_uses_repeated_real_learning(self):
        self._record_strategy_success("openalex", count=2)
        names = [provider.name for provider in _default_providers(self.root)]
        self.assertEqual(names[0], "openalex")
        self.assertEqual(set(names), {"arxiv", "crossref", "openalex"})

    def test_opportunity_provider_order_ignores_single_event(self):
        self._record_strategy_success("openalex", count=1)
        names = [provider.name for provider in _default_providers(self.root)]
        self.assertEqual(names, ["arxiv", "crossref", "openalex"])


if __name__ == "__main__":
    unittest.main()
