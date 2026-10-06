"""Preserve per-source audit and run status while simplifying read orchestration."""

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.autonomous_promotion import _branch_points_for_symbol
from sira.reading import Document, ExtractBatch, read_run


class _Reader:
    name = "fixture_reader"
    cache_namespace = "fixture_reader_v1"

    def __init__(self, *, cancel=False):
        self.calls = []
        self.cancel = cancel

    def read(self, urls):
        self.calls.append(urls)
        if self.cancel:
            raise KeyboardInterrupt()
        return ExtractBatch(
            documents=(Document(urls[0], "Virtual environments isolate Python packages.",
                                "2025-09-25T00:00:00Z"),),
            failures={},
            api_requests=0,
            credits=0,
        )


class ReadingEvidenceAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.parent_id = "a" * 32

    def run_with_sources(self, sources, reader):
        parent = self.root / "runs" / self.parent_id / "result.json"
        parent.parent.mkdir(parents=True, exist_ok=True)
        parent.write_text(json.dumps({
            "schema_version": 1, "run_id": self.parent_id,
            "question": "What do virtual environments isolate?", "sources": sources,
        }), encoding="utf-8")
        path = read_run(self.root, self.parent_id, reader, use_cache=False)
        return path, json.loads((path / "evidence.json").read_text(encoding="utf-8"))

    def test_safe_and_unsafe_sources_keep_order_and_provenance(self):
        safe_url = "https://example.org/python"
        reader = _Reader()
        path, report = self.run_with_sources([
            {"id": "S1", "url": safe_url, "title": "Python"},
            {"id": "S2", "url": "http://127.0.0.1/private", "title": "Private"},
        ], reader)

        self.assertEqual(reader.calls, [(safe_url,)])
        self.assertEqual(report["status"], "partial")
        self.assertEqual([row["status"] for row in report["documents"]], ["read", "unsafe_url"])
        self.assertEqual(report["documents"][0]["trust"], "untrusted")
        self.assertEqual(report["documents"][0]["content_origin"], "fixture_reader")
        self.assertEqual((path / "documents/S1.txt").read_text(encoding="utf-8"),
                         "Virtual environments isolate Python packages.")
        self.assertTrue(all(item["source_id"] == "S1" for item in report["evidence"]))
        events = [json.loads(line)["event"] for line in (path / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(events[-3:], ["source_finished", "source_finished", "run_finished"])

    def test_empty_and_cancelled_runs_report_distinct_statuses(self):
        reader = _Reader()
        _, empty = self.run_with_sources([], reader)
        self.assertEqual(reader.calls, [])
        self.assertEqual(empty["status"], "no_sources")
        self.assertEqual(empty["documents"], [])

        cancelling = _Reader(cancel=True)
        _, stopped = self.run_with_sources([
            {"id": "S1", "url": "https://example.org/python", "title": "Python"},
        ], cancelling)
        self.assertEqual(cancelling.calls, [("https://example.org/python",)])
        self.assertEqual(stopped["status"], "cancelled")
        self.assertEqual(stopped["error"], {"code": "user_cancelled"})
        self.assertIsNone(stopped["metrics"]["api_requests"])

    def test_read_run_meets_local_branch_goal(self):
        self.assertLessEqual(
            _branch_points_for_symbol(ROOT, "src/sira/reading.py", "read_run"),
            29,  # Baseline 33; one local opportunity requires four fewer branches.
        )


if __name__ == "__main__":
    unittest.main()
