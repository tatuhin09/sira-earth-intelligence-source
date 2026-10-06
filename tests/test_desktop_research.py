from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sira.desktop_chat import (
    DesktopChatSettingsStore,
)
from sira.desktop_research import (
    DesktopResearchJobStore,
    DesktopResearchSettingsStore,
    classify_research_domain,
    create_research_job,
    explicit_research_requested,
    research_settings_status,
    recover_interrupted_research_jobs,
    run_research_job,
)
from sira.self_modification import PROTECTED_PATHS


class DesktopResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_research_boundary_is_protected(self):
        self.assertIn(
            "src/sira/desktop_research.py",
            PROTECTED_PATHS,
        )

    def test_explicit_intent_detection_does_not_trigger_normal_chat(self):
        self.assertTrue(
            explicit_research_requested(
                "Research evidence verification systems"
            )
        )
        self.assertTrue(
            explicit_research_requested(
                "এই topic রিসার্চ করো"
            )
        )
        self.assertFalse(
            explicit_research_requested(
                "Evidence synthesis কী?"
            )
        )

    def test_domain_classification(self):
        self.assertEqual(
            classify_research_domain(
                "Find research papers about AI verification"
            ),
            "scholarly",
        )
        self.assertEqual(
            classify_research_domain(
                "Research clinical diabetes treatment"
            ),
            "biomedical",
        )
        self.assertEqual(
            classify_research_domain(
                "Research latest Python release changes"
            ),
            "general_web",
        )

    def test_cli_summary_loads_actual_papers_artifact(self):
        from sira.desktop_research import (
            _load_papers_artifact_from_cli_summary,
        )

        run_id = "a" * 32
        run_dir = self.root / "runs" / run_id
        run_dir.mkdir(parents=True)
        artifact = {
            "schema_version": 1,
            "run_id": run_id,
            "question": "evidence verification in AI systems",
            "status": "completed",
            "papers": [
                {
                    "paper_id": "p1",
                    "title": "Paper One",
                    "abstract": "Evidence one.",
                    "authors": ["A"],
                    "year": 2025,
                    "doi": "10.1/one",
                    "url": "https://example.org/one",
                    "open_access_pdf_url": None,
                    "retrieved_at": "2026-09-21T00:00:00+00:00",
                    "provider": "semantic_scholar",
                    "published_date": None,
                    "providers": ["semantic_scholar"],
                },
                {
                    "paper_id": "p2",
                    "title": "Paper Two",
                    "abstract": "Evidence two.",
                    "authors": ["B"],
                    "year": 2024,
                    "doi": "10.1/two",
                    "url": "https://example.net/two",
                    "open_access_pdf_url": None,
                    "retrieved_at": "2026-09-21T00:00:00+00:00",
                    "provider": "crossref",
                    "published_date": None,
                    "providers": ["crossref"],
                },
            ],
            "metrics": {
                "paper_count": 2,
                "api_requests": 2,
            },
        }
        path = run_dir / "papers.json"
        path.write_text(
            json.dumps(artifact),
            encoding="utf-8",
        )
        summary = {
            "run_id": run_id,
            "status": "completed",
            "papers": 2,
            "metrics": artifact["metrics"],
            "error": None,
            "result": str(path),
        }

        loaded = _load_papers_artifact_from_cli_summary(
            self.root,
            summary,
        )
        self.assertEqual(len(loaded["papers"]), 2)
        self.assertEqual(
            loaded["papers"][0]["title"],
            "Paper One",
        )

    def test_cli_summary_rejects_artifact_outside_runs(self):
        from sira.desktop_research import (
            _load_papers_artifact_from_cli_summary,
        )

        run_id = "b" * 32
        outside = self.root / "papers.json"
        outside.write_text(
            json.dumps({
                "schema_version": 1,
                "run_id": run_id,
                "question": "x",
                "status": "completed",
                "papers": [],
                "metrics": {},
            }),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            _load_papers_artifact_from_cli_summary(
                self.root,
                {
                    "run_id": run_id,
                    "status": "completed",
                    "papers": 0,
                    "result": str(outside),
                },
            )

    def test_scholarly_route_is_free_first_and_multi_evidence_verified(self):
        job = create_research_job(
            self.root,
            "Research papers on evidence verification",
        )

        def fake_papers(_root, _query):
            return {
                "status": "completed",
                "papers": [
                    {
                        "title": "Paper One",
                        "abstract": "Evidence one.",
                        "doi": "10.1/one",
                        "url": "https://example.org/one",
                        "year": 2025,
                        "provider": "semantic_scholar",
                    },
                    {
                        "title": "Paper Two",
                        "abstract": "Evidence two.",
                        "doi": "10.1/two",
                        "url": "https://example.net/two",
                        "year": 2024,
                        "provider": "crossref",
                    },
                ],
                "metrics": {
                    "api_requests": 2,
                },
            }

        with patch(
            "sira.desktop_research.research_mesh_plan",
            return_value={
                "status": "ready",
                "paid_spending": False,
            },
        ):
            result = run_research_job(
                self.root,
                job["job_id"],
                papers_runner=fake_papers,
            )

        self.assertEqual(
            result["route"],
            "free_scholarly_papers",
        )
        self.assertEqual(
            result["verification"]["status"],
            "verified_multi_evidence",
        )
        self.assertEqual(
            result["metrics"]["paid_requests"],
            0,
        )
        self.assertEqual(len(result["sources"]), 2)

    def test_general_web_blocks_without_search_confirmation(self):
        job = create_research_job(
            self.root,
            "Research latest Python security changes",
        )
        with patch(
            "sira.desktop_research.research_mesh_plan",
            return_value={
                "status": "no_free_route",
                "paid_spending": False,
            },
        ):
            result = run_research_job(
                self.root,
                job["job_id"],
            )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            result["progress"],
            "search_confirmation_required",
        )

    def test_general_web_grounding_verifies_distinct_hosts(self):
        DesktopChatSettingsStore(
            self.root
        ).set_free_tier_confirmed(True)
        DesktopResearchSettingsStore(
            self.root
        ).set_search_zero_cost_confirmed(True)
        job = create_research_job(
            self.root,
            "Research latest Python security changes",
        )

        def fake_web(_root, _query, **_kwargs):
            return {
                "status": "completed",
                "result": {
                    "text": "Grounded answer [S1] [S2].",
                    "sources": [
                        {
                            "url": "https://python.org/a",
                            "title": "Python",
                            "source_kind": "google_search",
                        },
                        {
                            "url": "https://nvd.nist.gov/b",
                            "title": "NVD",
                            "source_kind": "google_search",
                        },
                    ],
                },
                "metrics": {
                    "api_requests": 1,
                },
            }

        with patch(
            "sira.desktop_research.research_mesh_plan",
            return_value={
                "status": "no_free_route",
                "paid_spending": False,
            },
        ):
            result = run_research_job(
                self.root,
                job["job_id"],
                web_runner=fake_web,
            )
        self.assertEqual(
            result["status"],
            "completed",
        )
        self.assertEqual(
            result["verification"]["status"],
            "verified_multi_source",
        )
        self.assertEqual(
            result["route"],
            "guarded_gemini_search",
        )
        self.assertFalse(result["paid_spending"])

    def test_restart_recovery_closes_orphaned_job_without_retry(self):
        job = create_research_job(
            self.root,
            "Research latest Python security changes",
        )
        recovery = recover_interrupted_research_jobs(
            self.root
        )
        self.assertEqual(recovery["recovered_count"], 1)
        self.assertFalse(recovery["automatic_retry"])
        recovered = DesktopResearchJobStore(
            self.root
        ).read(job["job_id"])
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(
            recovered["progress"],
            "interrupted_by_desktop_restart",
        )

    def test_settings_never_grant_paid_or_runtime_authority(self):
        status = research_settings_status(self.root)
        self.assertTrue(status["explicit_only"])
        self.assertTrue(status["free_first"])
        self.assertFalse(
            status["paid_spending_authority"]
        )
        self.assertFalse(
            status["runtime_control_authority"]
        )
        self.assertFalse(
            status["promotion_authority"]
        )


if __name__ == "__main__":
    unittest.main()
