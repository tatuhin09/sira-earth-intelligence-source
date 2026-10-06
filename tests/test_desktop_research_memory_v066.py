from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.desktop_app import DesktopControl
from sira.desktop_research import DesktopResearchJobStore, create_research_job, run_research_job
from sira.memory import MemoryStore
from sira.memory_integrity import build_integrity_report


def papers(count: int) -> dict:
    return {
        "status": "completed",
        "papers": [
            {
                "title": f"Public evidence paper {i}",
                "abstract": "Independent public findings.",
                "doi": f"10.1/{i}",
                "url": f"https://example{i}.org/paper",
                "provider": "crossref",
            }
            for i in range(count)
        ],
        "metrics": {"api_requests": 0},
    }


class DesktopResearchMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def research(self, count: int = 2):
        job = create_research_job(self.root, "Research papers on private personal question about evidence")
        with patch("sira.desktop_research.research_mesh_plan", return_value={"status": "ready"}):
            result = run_research_job(self.root, job["job_id"], papers_runner=lambda *_: papers(count))
        return result

    def test_verified_job_becomes_researched_source_memory_once_with_provenance(self):
        from sira.desktop_research_memory import ingest_verified_desktop_research
        job = self.research()
        path = DesktopResearchJobStore(self.root).path(job["job_id"])
        original_bytes = path.read_bytes()
        original_sha = hashlib.sha256(original_bytes).hexdigest()

        first = ingest_verified_desktop_research(self.root, job["job_id"])
        second = ingest_verified_desktop_research(self.root, job["job_id"])

        self.assertEqual(first["status"], "learned")
        self.assertEqual(first["occurrences_added"], 1)
        self.assertEqual(second["occurrences_added"], 0)
        memory_store = MemoryStore(self.root)
        self.assertEqual(memory_store.stats()["total_memories"], 1)
        integrity = build_integrity_report(memory_store.db_path, memory_store.backup_path)
        self.assertTrue(integrity.migration_ready)
        self.assertEqual(integrity.primary.counts["occurrences"], 1)
        row = MemoryStore(self.root).retrieve("Public evidence paper", 5)[0]
        self.assertEqual(row["status"], "researched")
        self.assertNotIn("private personal question", row["summary"])
        self.assertNotIn("Independent public findings", row["summary"])
        occurrence = MemoryStore(self.root).show(row["memory_id"])["occurrences"][0]
        self.assertEqual(occurrence["run_id"], job["job_id"])
        self.assertEqual(occurrence["artifact_sha256"], original_sha)
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_limited_or_tampered_source_evidence_does_not_enter_memory(self):
        from sira.desktop_research_memory import ingest_verified_desktop_research
        limited = self.research(1)
        self.assertEqual(ingest_verified_desktop_research(self.root, limited["job_id"])["status"], "skipped")
        verified = self.research(2)
        store = DesktopResearchJobStore(self.root)
        tampered = dict(verified)
        tampered["sources"] = tampered["sources"][:1]
        store.save(tampered)
        self.assertEqual(ingest_verified_desktop_research(self.root, verified["job_id"])["status"], "skipped")
        self.assertEqual(MemoryStore(self.root).stats()["total_memories"], 0)

    def test_distinct_grounded_web_hosts_are_indexed_without_answer_text(self):
        from sira.desktop_research_memory import ingest_verified_desktop_research
        job = create_research_job(self.root, "Research a private web question")
        job.update({
            "status": "completed",
            "route": "guarded_gemini_search",
            "answer": "PRIVATE SYNTHESIZED ANSWER",
            "sources": [
                {"source_id": "S1", "title": "Official public reference", "url": "https://alpha.example/ref"},
                {"source_id": "S2", "title": "Public technical reference", "url": "https://beta.example/ref"},
            ],
            "verification": {
                "status": "verified_multi_source", "method": "distinct grounded web hosts",
                "source_count": 2, "independent_evidence_count": 2,
            },
        })
        DesktopResearchJobStore(self.root).save(job)
        result = ingest_verified_desktop_research(self.root, job["job_id"])
        self.assertEqual(result["status"], "learned")
        memory = MemoryStore(self.root).show(result["memory_id"])
        self.assertEqual(memory["status"], "researched")
        self.assertNotIn("PRIVATE", memory["summary"])
        self.assertNotIn("private web question", memory["summary"])

    def test_desktop_restart_reconciles_completed_research_and_live_worker_learns(self):
        old = self.research(2)
        control = DesktopControl(self.root)
        self.assertEqual(MemoryStore(self.root).stats()["total_memories"], 1)
        self.assertEqual(control.research_memory_reconciliation["learned"], 1)
        fresh = create_research_job(self.root, "Research papers on another private evidence question")

        def run(_root, job_id, *, history):
            with patch("sira.desktop_research.research_mesh_plan", return_value={"status": "ready"}):
                return run_research_job(self.root, job_id, history=history, papers_runner=lambda *_: papers(2))

        with patch("sira.desktop_app.run_research_job", side_effect=run):
            control._research_worker(fresh["job_id"], [])
        self.assertEqual(MemoryStore(self.root).stats()["total_memories"], 2)
        self.assertEqual(DesktopControl(self.root).research_memory_reconciliation["learned"], 0)
        self.assertTrue(MemoryStore(self.root).retrieve("Public evidence paper", 5))
        self.assertEqual(old["status"], "completed")

    def test_memory_failure_preserves_research_and_tells_owner_without_raw_error(self):
        job = self.research(2)
        control = DesktopControl(self.root)
        with patch("sira.desktop_app.run_research_job", return_value=job), patch(
            "sira.desktop_app.ingest_verified_desktop_research",
            side_effect=OSError("secret internal path"),
        ):
            control._research_worker(job["job_id"], [])
        messages = [item["text"] for item in control.chat.load()]
        self.assertTrue(any("memory" in text.casefold() and "failed" in text.casefold()
                            for text in messages))
        self.assertFalse(any("secret internal path" in text for text in messages))
        self.assertEqual(DesktopResearchJobStore(self.root).read(job["job_id"])["status"], "completed")


if __name__ == "__main__":
    unittest.main()
