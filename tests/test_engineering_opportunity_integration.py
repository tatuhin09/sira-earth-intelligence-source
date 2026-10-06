from pathlib import Path
import hashlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.opportunity_evidence import build_opportunity_evidence


class EngineeringOpportunityIntegrationTests(unittest.TestCase):
    def test_evidence_contains_non_authoritative_engineering_profile_and_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src" / "sira" / "demo.py"
            source.parent.mkdir(parents=True)
            source.write_text("def demo():\n    return 1\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_demo.py").write_text(
                "from sira.demo import demo\n\ndef test_demo():\n    assert demo() == 1\n",
                encoding="utf-8",
            )
            payload = source.read_bytes()
            opportunity = {
                "opportunity_id": "op_" + "a" * 32,
                "fingerprint": "b" * 64,
                "type": "missing_test_reference",
                "path": "src/sira/demo.py",
                "symbol": "demo",
                "summary": "fixture",
                "evidence": {"line": 1, "test_reference_count": 0},
                "priority_score": 10,
                "source_sha256": hashlib.sha256(payload).hexdigest(),
            }
            with patch("sira.opportunity_evidence._load_opportunity", return_value=opportunity):
                report = build_opportunity_evidence(root, opportunity["opportunity_id"])
            profile = report["engineering_profile"]
            plan = report["verification_plan"]
            self.assertEqual(profile["primary_language"], "python")
            self.assertEqual(plan["target_language"], "python")
            self.assertFalse(profile["authority_granted"])
            self.assertFalse(plan["promotion_authorized"])
            self.assertFalse(plan["execution_performed"])


if __name__ == "__main__":
    unittest.main()
