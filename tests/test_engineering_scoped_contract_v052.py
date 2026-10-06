from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.engineering_writer import build_engineering_writer_context
from sira.providers.gemini_engineering import GeminiEngineeringModel


SOURCE = """class Demo:
    def target(self, value: int) -> int:
        if value > 0:
            return value
        return 0
"""


def scoped_task():
    return {
        "task_id": "scoped-contract-v052",
        "instruction": (
            "Improve src/demo.py:target with the smallest "
            "behavior-preserving change. Primary symbol: target."
        ),
        "target_paths": ["src/demo.py"],
        "target": {"path": "src/demo.py", "symbol": "target"},
        "language_hint": "python",
        "success_criteria": {},
        "diagnostics": [],
    }


class EngineeringScopedContractV052Tests(unittest.TestCase):
    def test_scoped_context_forbids_sibling_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "demo.py").write_text(
                SOURCE,
                encoding="utf-8",
            )
            context = build_engineering_writer_context(
                root,
                scoped_task(),
            )

        contract = context["scoped_edit"]["contract"].casefold()
        self.assertIn("do not add sibling helper definitions", contract)

    def test_gemini_request_repeats_sibling_helper_prohibition(self):
        captured = {}

        def fake_request_json(
            request,
            timeout,
            opener,
            *,
            max_bytes,
        ):
            captured["body"] = json.loads(
                request.data.decode("utf-8")
            )
            return {
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": json.dumps({
                        "summary": "fixture",
                        "edits": [],
                    })}]},
                }],
                "usageMetadata": {
                    "promptTokenCount": 1,
                    "candidatesTokenCount": 1,
                },
            }

        model = GeminiEngineeringModel(
            "unit-test-key",
            max_attempts=1,
        )
        with patch(
            "sira.providers.gemini_engineering.request_json",
            side_effect=fake_request_json,
        ):
            model.generate_patch(
                {
                    "scoped_edit": {
                        "mode": "python_symbol_block",
                    },
                    "files": [],
                },
                {"type": "object", "properties": {}},
            )

        instruction = captured["body"]["systemInstruction"][
            "parts"
        ][0]["text"].casefold()
        self.assertIn("do not add sibling helper definitions", instruction)


if __name__ == "__main__":
    unittest.main()
