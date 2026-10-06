from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.autonomous_targeting import select_autonomous_target
from sira.opportunity import discover_opportunities


def _complex(name: str, branches: int) -> str:
    lines = [f"def {name}(value):"]
    for number in range(branches):
        lines.extend([
            f"    if value == {number}:",
            f"        value += {number + 1}",
        ])
    lines.append("    return value")
    return "\n".join(lines) + "\n"


class AutonomousReadinessPreselectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "src/sira").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "src/sira/engineering_evaluator.py").write_text(
            _complex("evaluate_engineering_candidate", 24), encoding="utf-8"
        )
        (self.root / "src/sira/memory.py").write_text(
            _complex("memory_probe", 20), encoding="utf-8"
        )
        (self.root / "tests/test_memory_probe.py").write_text(
            "from sira.memory import memory_probe\n"
            "def test_memory_probe():\n    assert memory_probe(0) >= 0\n",
            encoding="utf-8",
        )
        (self.root / "tests/test_evaluator_probe.py").write_text(
            "from sira.engineering_evaluator import evaluate_engineering_candidate\n",
            encoding="utf-8",
        )

    def _select(self):
        return select_autonomous_target(
            self.root, now_epoch=2_000_000_000.0,
            memory_candidates_fn=lambda *_: [],
            knowledge_candidates_fn=lambda *_: [],
        )

    def test_autonomous_selection_skips_unmapped_high_score_without_hiding_discovery(self):
        discovered = discover_opportunities(
            self.root, now_epoch=2_000_000_000.0, limit=20,
        )
        self.assertEqual(
            discovered["opportunities"][0]["path"],
            "src/sira/engineering_evaluator.py",
        )
        report = self._select()
        self.assertEqual(report["target"]["path"], "src/sira/memory.py")
        self.assertEqual(report["target"]["symbol"], "memory_probe")
        self.assertTrue(any(
            x["path"] == "src/sira/engineering_evaluator.py"
            and x["reason"] == "benchmark_unmapped"
            for x in report["readiness_skipped"]
        ))
        self.assertEqual(report["api_requests"], 0)

    def test_missing_test_reference_skips_only_until_benchmark_and_caller_exist(self):
        from sira.opportunity_evidence import opportunity_readiness_preview

        item = {
            "path": "src/sira/memory.py", "symbol": "memory_probe",
            "type": "missing_test_reference",
        }
        self.assertEqual(opportunity_readiness_preview(self.root, item)["reason"],
                         "caller_missing")
        (self.root / "src/sira/caller.py").write_text(
            "from .memory import memory_probe\n"
            "def caller(value):\n    return memory_probe(value)\n",
            encoding="utf-8",
        )
        self.assertTrue(opportunity_readiness_preview(self.root, item)["ready"])
        self.assertEqual(opportunity_readiness_preview(self.root, {
            "path": "src/sira/engineering_evaluator.py",
            "symbol": "evaluate_engineering_candidate", "type": "complex_function",
        })["reason"], "benchmark_unmapped")

    def test_ready_opportunity_below_first_twenty_can_be_selected(self):
        for index in range(20):
            (self.root / f"src/sira/unmapped_{index}.py").write_text(
                _complex(f"unmapped_{index}", 28), encoding="utf-8"
            )
        (self.root / "src/sira/memory.py").write_text(
            _complex("memory_probe", 20), encoding="utf-8"
        )
        discovered = discover_opportunities(
            self.root, now_epoch=2_000_000_000.0, limit=20,
        )
        self.assertFalse(any(row["path"] == "src/sira/memory.py"
                             for row in discovered["opportunities"]))

        selected = self._select()
        self.assertEqual(selected["status"], "selected")
        self.assertEqual(selected["target"]["path"], "src/sira/memory.py")
        self.assertEqual(selected["target"]["symbol"], "memory_probe")
        self.assertEqual(selected["api_requests"], 0)


if __name__ == "__main__":
    unittest.main()
