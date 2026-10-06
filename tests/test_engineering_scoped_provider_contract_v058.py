"""The scoped model must receive the local helper's callable interface."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.engineering_scoped_edit import compact_engineering_model_context
from sira.engineering_writer import build_engineering_writer_context
from sira.providers.gemini_engineering import GeminiEngineeringModel
import sira.providers.gemini_engineering as gemini_engineering


class EngineeringScopedProviderContractV058Tests(unittest.TestCase):
    def test_request_requires_matching_keyword_only_helper_interface(self):
        source = '''def _base_cluster_key(*, kind, category, capability, provider):
    """PRIVATE_HELPER_IMPLEMENTATION"""
    return kind + category + capability + (provider or "")


class MemoryStore:
    @staticmethod
    def _rebuild_clusters(conn):
        row = conn.execute("SELECT * FROM memories").fetchone()
        return _base_cluster_key(
            kind=row["kind"], category=row["category"],
            capability=row["capability"], provider=row["provider"],
        )
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "src/sira/memory.py"
            path.parent.mkdir(parents=True)
            path.write_text(source, encoding="utf-8")
            context = build_engineering_writer_context(root, {
                "task_id": "a11-replay-interface",
                "instruction": "Improve _rebuild_clusters while preserving its behavior.",
                "target_paths": ["src/sira/memory.py"],
                "target": {"path": "src/sira/memory.py", "symbol": "_rebuild_clusters"},
                "language_hint": "python",
                "success_criteria": {},
                "diagnostics": [],
            })
            model_input = compact_engineering_model_context(context)

        captured = {}

        def fake_request_json(request, timeout, opener, *, max_bytes):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return {
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": json.dumps({"summary": "ok", "edits": []})}]},
                }],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
            }

        with patch.object(gemini_engineering, "request_json", side_effect=fake_request_json):
            GeminiEngineeringModel("unit-test-key", max_attempts=1).generate_patch(
                model_input, {"type": "object", "properties": {}},
            )

        body = captured["body"]
        instruction = body["systemInstruction"]["parts"][0]["text"]
        self.assertIn("referenced_signatures", instruction)
        self.assertIn("keyword-only", instruction)
        self.assertIn("positional-only", instruction)
        supplied = json.loads(body["contents"][0]["parts"][0]["text"])["input"]
        self.assertEqual(supplied["scoped_edit"]["referenced_signatures"], [
            {"name": "_base_cluster_key",
             "signature": "def _base_cluster_key(*, kind, category, capability, provider)"},
        ])
        self.assertNotIn("PRIVATE_HELPER_IMPLEMENTATION", json.dumps(supplied))


if __name__ == "__main__":
    unittest.main()
