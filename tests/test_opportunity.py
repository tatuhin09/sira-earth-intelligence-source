import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.cli import main


class OpportunityDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src" / "sira").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "benchmarks").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "sira.py").write_text("print('fixture')\n", encoding="utf-8")
        (self.root / "README.md").write_text("fixture\n", encoding="utf-8")
        (self.root / ".env.example").write_text("\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _complex_module(marker="v1"):
        body = ["def hard_path(value):"]
        body.append(f"    marker = {marker!r}")
        for idx in range(20):
            body.extend([
                f"    if value == {idx}:",
                f"        value += {idx + 1}",
            ])
        body.append("    return value, marker")
        body.extend([""] * 3)
        body.extend([f"# filler {i}" for i in range(470)])
        return "\n".join(body) + "\n"

    def test_local_discovery_ranks_evidence_and_excludes_protected_shell(self):
        from sira.opportunity import discover_opportunities

        (self.root / "src" / "sira" / "candidate.py").write_text(self._complex_module(), encoding="utf-8")
        (self.root / "src" / "sira" / "runtime.py").write_text(self._complex_module("protected"), encoding="utf-8")

        result = discover_opportunities(self.root, limit=10, now_epoch=1_800_000_000)

        self.assertEqual(result["kind"], "opportunity_discovery")
        self.assertEqual(result["api_requests"], 0)
        self.assertGreaterEqual(result["scan"]["protected_files_skipped"], 1)
        self.assertTrue(result["opportunities"])
        self.assertTrue(all(row["path"] == "src/sira/candidate.py" for row in result["opportunities"]))
        self.assertTrue(all(row["priority_score"] > 0 for row in result["opportunities"]))
        self.assertTrue(all(len(row["fingerprint"]) == 64 for row in result["opportunities"]))
        self.assertTrue(Path(result["artifact"]).is_file())

    def test_fingerprint_is_stable_until_source_changes(self):
        from sira.opportunity import discover_opportunities

        path = self.root / "src" / "sira" / "candidate.py"
        path.write_text(self._complex_module("v1"), encoding="utf-8")
        first = discover_opportunities(self.root, limit=10, now_epoch=1_800_000_000)
        second = discover_opportunities(self.root, limit=10, now_epoch=1_800_000_001)
        first_ids = [(x["type"], x.get("symbol"), x["fingerprint"]) for x in first["opportunities"]]
        second_ids = [(x["type"], x.get("symbol"), x["fingerprint"]) for x in second["opportunities"]]
        self.assertEqual(first_ids, second_ids)

        path.write_text(self._complex_module("v2"), encoding="utf-8")
        changed = discover_opportunities(self.root, limit=10, now_epoch=1_800_000_002)
        changed_ids = [(x["type"], x.get("symbol"), x["fingerprint"]) for x in changed["opportunities"]]
        self.assertNotEqual(first_ids, changed_ids)

    def test_attempt_cooldown_suppresses_same_fingerprint_but_not_changed_source(self):
        from sira.opportunity import OpportunityStore, discover_opportunities

        path = self.root / "src" / "sira" / "candidate.py"
        path.write_text(self._complex_module("v1"), encoding="utf-8")
        first = discover_opportunities(self.root, limit=1, cooldown_seconds=3600, now_epoch=2_000_000_000)
        opportunity = first["opportunities"][0]
        OpportunityStore(self.root).mark_attempt(opportunity, "rejected_no_gain", attempted_at_epoch=2_000_000_000)

        cooling = discover_opportunities(self.root, limit=5, cooldown_seconds=3600, now_epoch=2_000_000_100)
        self.assertFalse(any(x["fingerprint"] == opportunity["fingerprint"] for x in cooling["opportunities"]))
        self.assertGreaterEqual(cooling["suppressed_cooldown_count"], 1)

        expired = discover_opportunities(self.root, limit=5, cooldown_seconds=3600, now_epoch=2_000_003_601)
        self.assertTrue(any(x["fingerprint"] == opportunity["fingerprint"] for x in expired["opportunities"]))

        path.write_text(self._complex_module("v2"), encoding="utf-8")
        changed = discover_opportunities(self.root, limit=5, cooldown_seconds=3600, now_epoch=2_000_000_200)
        self.assertTrue(changed["opportunities"])
        self.assertTrue(all(x["fingerprint"] != opportunity["fingerprint"] for x in changed["opportunities"]))

    def test_cli_improve_opportunities_outputs_json_without_network(self):
        (self.root / "src" / "sira" / "candidate.py").write_text(self._complex_module(), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--root", str(self.root), "improve", "opportunities", "--limit", "3"])
        self.assertEqual(code, 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["kind"], "opportunity_discovery")
        self.assertEqual(data["api_requests"], 0)
        self.assertLessEqual(len(data["opportunities"]), 3)


if __name__ == "__main__":
    unittest.main()
