from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sira.models import ProviderError
from sira.providers import gemini_engineering
from sira.providers.gemini_engineering import GeminiEngineeringModel


class GeminiEngineeringOutputBudgetV048Tests(unittest.TestCase):
    def test_request_uses_model_supported_large_output_budget(self):
        captured = {}

        def fake_request_json(request, timeout, opener, *, max_bytes):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            captured["max_bytes"] = max_bytes
            return {
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "{}"}]},
                }],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 2,
                },
            }

        model = GeminiEngineeringModel("unit-test-key", max_attempts=1)
        with patch.object(
            gemini_engineering,
            "request_json",
            side_effect=fake_request_json,
        ):
            batch = model.generate_patch(
                {"files": [], "editable_path_policy": {}},
                {"type": "object"},
            )

        config = captured["body"]["generationConfig"]
        self.assertEqual(gemini_engineering.MAX_OUTPUT_TOKENS, 65536)
        self.assertEqual(config["maxOutputTokens"], 65536)
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertEqual(config["responseJsonSchema"], {"type": "object"})
        self.assertEqual(batch.attempt_count, 1)
        self.assertEqual(batch.api_requests, 1)
        self.assertEqual(batch.input_tokens, 10)
        self.assertEqual(batch.output_tokens, 2)

        encoded = json.dumps(captured["body"], ensure_ascii=False)
        self.assertNotIn("unit-test-key", encoded)

    def test_output_truncation_remains_non_retryable_and_sanitized(self):
        calls = 0

        def fake_request_json(request, timeout, opener, *, max_bytes):
            nonlocal calls
            calls += 1
            return {
                "candidates": [{
                    "finishReason": "MAX_TOKENS",
                    "content": {"parts": [{"text": "{partial"}]},
                }],
            }

        model = GeminiEngineeringModel("unit-test-key", max_attempts=3)
        with patch.object(
            gemini_engineering,
            "request_json",
            side_effect=fake_request_json,
        ):
            with self.assertRaises(ProviderError) as ctx:
                model.generate_patch({}, {"type": "object"})

        exc = ctx.exception
        self.assertEqual(getattr(exc, "code", None), "output_truncated")
        self.assertEqual(getattr(exc, "request_count", None), 1)
        self.assertEqual(calls, 1)
        self.assertNotIn("partial", str(exc))


if __name__ == "__main__":
    unittest.main()
