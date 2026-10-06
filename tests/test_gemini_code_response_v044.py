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
from sira.providers.gemini_code import GeminiCodeModel, MAX_OUTPUT_TOKENS

SCHEMA = {
    "type": "object",
    "required": ["summary", "edits"],
    "properties": {
        "summary": {"type": "string"},
        "edits": {"type": "array", "items": {"type": "object"}},
    },
}


class GeminiCodeResponseResilienceTests(unittest.TestCase):
    def _model(self):
        return GeminiCodeModel(
            "test-key",
            sleep_fn=lambda _seconds: None,
            jitter_fn=lambda _a, _b: 0.0,
        )

    def test_request_uses_model_supported_large_output_budget(self):
        captured = {}

        def fake_request_json(request, timeout, opener, max_bytes):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return {
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": json.dumps({
                        "summary": "no change",
                        "edits": [],
                    })}]},
                }],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 20,
                },
            }

        with patch(
            "sira.providers.gemini_code.request_json",
            side_effect=fake_request_json,
        ):
            batch = self._model().generate_patch({"files": []}, SCHEMA)

        self.assertEqual(MAX_OUTPUT_TOKENS, 65536)
        self.assertEqual(
            captured["body"]["generationConfig"]["maxOutputTokens"],
            MAX_OUTPUT_TOKENS,
        )
        self.assertEqual(batch.data["edits"], [])
        self.assertEqual(batch.output_tokens, 20)

    def test_max_tokens_finish_reason_is_output_truncated(self):
        response = {
            "candidates": [{
                "finishReason": "MAX_TOKENS",
                "content": {"parts": [{"text": '{"summary":"cut'}]},
            }],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 65536,
            },
        }
        with patch(
            "sira.providers.gemini_code.request_json",
            return_value=response,
        ):
            with self.assertRaises(ProviderError) as ctx:
                self._model().generate_patch({"files": []}, SCHEMA)
        self.assertEqual(ctx.exception.code, "output_truncated")
        self.assertTrue(ctx.exception.request_sent)
        self.assertEqual(ctx.exception.request_count, 1)

    def test_malformed_stop_response_preserves_attempt_count(self):
        calls = {"count": 0}

        def fake_request_json(*_args, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise ProviderError("http_503", True, request_count=1)
            return {
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "{not-json"}]},
                }],
            }

        with patch(
            "sira.providers.gemini_code.request_json",
            side_effect=fake_request_json,
        ):
            with self.assertRaises(ProviderError) as ctx:
                self._model().generate_patch({"files": []}, SCHEMA)
        self.assertEqual(ctx.exception.code, "malformed_json")
        self.assertEqual(ctx.exception.request_count, 2)
        self.assertEqual(calls["count"], 2)

    def test_safety_finish_is_response_blocked(self):
        response = {
            "candidates": [{
                "finishReason": "SAFETY",
                "content": {"parts": []},
            }]
        }
        with patch(
            "sira.providers.gemini_code.request_json",
            return_value=response,
        ):
            with self.assertRaises(ProviderError) as ctx:
                self._model().generate_patch({"files": []}, SCHEMA)
        self.assertEqual(ctx.exception.code, "response_blocked")
        self.assertEqual(ctx.exception.request_count, 1)


if __name__ == "__main__":
    unittest.main()
