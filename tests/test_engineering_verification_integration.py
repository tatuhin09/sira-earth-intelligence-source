from pathlib import Path
import hashlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.opportunity_evidence import build_opportunity_evidence


class EngineeringVerificationIntegrationTests(unittest.TestCase):
    def test_opportunity_evidence_carries_non_authoritative_execution_contract(self):
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
            opportunity = {
                "opportunity_id": "op_" + "a" * 32,
                "fingerprint": "b" * 64,
                "type": "missing_test_reference",
                "path": "src/sira/demo.py",
                "symbol": "demo",
                "summary": "fixture",
                "evidence": {"line": 1, "test_reference_count": 0},
                "priority_score": 10,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
            with patch(
                "sira.opportunity_evidence._load_opportunity",
                return_value=opportunity,
            ):
                report = build_opportunity_evidence(root, opportunity["opportunity_id"])
            contract = report["verification_execution_contract"]
            self.assertIn(contract["status"], {"ready", "missing_tool"})
            self.assertTrue(contract["network_namespace_required"])
            self.assertFalse(contract["execution_performed"])
            self.assertFalse(contract["authority_granted"])
            self.assertFalse(contract["promotion_authorized"])


if __name__ == "__main__":
    unittest.main()
