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
from sira.providers.gemini_code import GeminiCodeModel
from sira.code_writer import CODE_PATCH_SCHEMA


class GeminiResponseDiagnosticsV046Tests(unittest.TestCase):
    def _model(self):
        return GeminiCodeModel(
            "test-key",
            sleep_fn=lambda _seconds: None,
            jitter_fn=lambda _a, _b: 0.0,
        )

    def _error_code(self, response):
        with patch(
            "sira.providers.gemini_code.request_json",
            return_value=response,
        ):
            with self.assertRaises(ProviderError) as ctx:
                self._model().generate_patch(
                    {"hypothesis": {"statement": "diagnostic"}, "files": []},
                    CODE_PATCH_SCHEMA,
                )
        self.assertTrue(ctx.exception.request_sent)
        self.assertEqual(ctx.exception.request_count, 1)
        return ctx.exception.code

    def test_missing_candidates_is_explicit(self):
        self.assertEqual(
            self._error_code({}),
            "missing_candidates",
        )
        self.assertEqual(
            self._error_code({"candidates": []}),
            "missing_candidates",
        )

    def test_invalid_candidate_shape_is_explicit(self):
        self.assertEqual(
            self._error_code({"candidates": ["bad"]}),
            "invalid_candidate_shape",
        )

    def test_missing_content_and_parts_are_explicit(self):
        self.assertEqual(
            self._error_code({
                "candidates": [{"finishReason": "STOP"}],
            }),
            "missing_content",
        )
        self.assertEqual(
            self._error_code({
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {},
                }],
            }),
            "missing_parts",
        )

    def test_empty_text_is_explicit(self):
        self.assertEqual(
            self._error_code({
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": []},
                }],
            }),
            "empty_response_text",
        )

    def test_malformed_json_is_explicit(self):
        self.assertEqual(
            self._error_code({
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "{bad-json"}]},
                }],
            }),
            "malformed_json",
        )

    def test_non_object_json_is_explicit(self):
        self.assertEqual(
            self._error_code({
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "[]"}]},
                }],
            }),
            "json_not_object",
        )

    def test_known_finish_reasons_are_sanitized_codes(self):
        cases = {
            "RECITATION": "response_recitation",
            "LANGUAGE": "unsupported_language",
            "OTHER": "generation_stopped_other",
            "MALFORMED_FUNCTION_CALL": "malformed_function_call",
            "IMAGE_SAFETY": "response_blocked",
        }
        for finish_reason, expected in cases.items():
            with self.subTest(finish_reason=finish_reason):
                self.assertEqual(
                    self._error_code({
                        "candidates": [{
                            "finishReason": finish_reason,
                            "content": {"parts": []},
                        }],
                    }),
                    expected,
                )

    def test_unknown_finish_reason_is_bounded_not_raw(self):
        code = self._error_code({
            "candidates": [{
                "finishReason": "SOME_NEW_REASON",
                "content": {"parts": []},
            }],
        })
        self.assertEqual(code, "generation_stopped_unknown")
        self.assertNotIn("SOME_NEW_REASON", code)

    def test_valid_structured_response_still_succeeds(self):
        response = {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": json.dumps({
                    "summary": "none",
                    "edits": [],
                    "needs_more_context": False,
                    "context_requests": [],
                })}]},
            }],
            "usageMetadata": {
                "promptTokenCount": 7,
                "candidatesTokenCount": 9,
            },
        }
        with patch(
            "sira.providers.gemini_code.request_json",
            return_value=response,
        ):
            batch = self._model().generate_patch(
                {"hypothesis": {"statement": "diagnostic"}, "files": []},
                CODE_PATCH_SCHEMA,
            )
        self.assertEqual(batch.api_requests, 1)
        self.assertEqual(batch.attempt_count, 1)
        self.assertEqual(batch.input_tokens, 7)
        self.assertEqual(batch.output_tokens, 9)
        self.assertEqual(batch.data["edits"], [])


if __name__ == "__main__":
    unittest.main()
