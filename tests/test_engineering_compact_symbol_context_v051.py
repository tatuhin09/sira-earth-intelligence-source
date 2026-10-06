from __future__ import annotations
import json
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.code_writer import CodeModelBatch
from sira.engineering_scoped_edit import compact_engineering_model_context
from sira.engineering_writer import EngineeringResearchBackedWriter, build_engineering_writer_context
import sira.providers.gemini_engineering as gemini_engineering
from sira.providers.gemini_engineering import GeminiEngineeringModel, MAX_OUTPUT_TOKENS, SCOPED_MAX_OUTPUT_TOKENS

SOURCE = """class Demo:\n    def target(self, value: int) -> int:\n        if value > 10:\n            return value\n        if value > 0:\n            return value + 1\n        return 0\n\n    def untouched(self) -> str:\n        return \"keep-me\"\n"""

def task():
    return {
        "task_id": "v051-test",
        "instruction": "Improve src/demo.py:target with the smallest behavior-preserving change. Primary symbol: target.",
        "target_paths": ["src/demo.py"],
        "target": {"path": "src/demo.py", "symbol": "target"},
        "language_hint": "python",
        "success_criteria": {"structural_goal": {"metric": "branch_points", "baseline": 2, "target_max": 1}},
        "diagnostics": [],
    }

class CaptureModel:
    name = "capture"
    model_id = "capture"
    def __init__(self): self.payloads = []
    def generate_patch(self, payload, schema):
        self.payloads.append(payload)
        return CodeModelBatch({
            "summary": "bounded replacement",
            "edits": [{
                "path": "src/demo.py",
                "symbol": "target",
                "content": "def target(self, value: int) -> int:\n    if value > 10:\n        return value\n    return value + 1 if value > 0 else 0\n",
                "reason": "reduce branches",
            }],
        })

class CompactSymbolModelContextV051Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "demo.py").write_text(SOURCE, encoding="utf-8")
    def tearDown(self): self.tmp.cleanup()

    def test_writer_sends_only_target_block_but_materializes_full_file(self):
        model = CaptureModel()
        writer = EngineeringResearchBackedWriter(self.root, model)
        edits = writer.propose_text_edits(task())
        sent = model.payloads[0]
        row = next(x for x in sent["files"] if x["path"] == "src/demo.py")
        self.assertEqual(row["model_context_scope"], "python_symbol_block")
        self.assertIn("def target(", row["content"])
        self.assertNotIn("def untouched(", row["content"])
        self.assertLess(row["model_content_bytes"], row["original_file_bytes"])
        self.assertLess(sent["context_bytes"], sent["local_context_bytes"])
        self.assertIn("def untouched(", edits["src/demo.py"])
        self.assertEqual((self.root / "src" / "demo.py").read_text(encoding="utf-8"), SOURCE)

    def test_compactor_does_not_mutate_local_context(self):
        context = build_engineering_writer_context(self.root, task())
        before = json.dumps(context, sort_keys=True)
        compact = compact_engineering_model_context(context)
        self.assertEqual(before, json.dumps(context, sort_keys=True))
        self.assertNotEqual(compact["context_sha256"], context["context_sha256"])

    def test_scoped_provider_uses_smaller_output_ceiling(self):
        seen = {}
        def fake_request_json(request, timeout, opener, *, max_bytes):
            body = json.loads(request.data.decode("utf-8"))
            seen["max"] = body["generationConfig"]["maxOutputTokens"]
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps({"summary": "ok", "edits": []})}]}}], "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}
        model = GeminiEngineeringModel("unit-test-key")
        with patch.object(gemini_engineering, "request_json", side_effect=fake_request_json):
            model.generate_patch({"scoped_edit": {"mode": "python_symbol_block"}, "files": []}, {"type": "object", "properties": {}})
        self.assertEqual(seen["max"], SCOPED_MAX_OUTPUT_TOKENS)
        self.assertLess(SCOPED_MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS)

    def test_legacy_provider_keeps_existing_output_ceiling(self):
        seen = {}
        def fake_request_json(request, timeout, opener, *, max_bytes):
            body = json.loads(request.data.decode("utf-8"))
            seen["max"] = body["generationConfig"]["maxOutputTokens"]
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps({"summary": "ok", "edits": []})}]}}], "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}
        model = GeminiEngineeringModel("unit-test-key")
        with patch.object(gemini_engineering, "request_json", side_effect=fake_request_json):
            model.generate_patch({"files": []}, {"type": "object", "properties": {}})
        self.assertEqual(seen["max"], MAX_OUTPUT_TOKENS)

if __name__ == "__main__": unittest.main()
