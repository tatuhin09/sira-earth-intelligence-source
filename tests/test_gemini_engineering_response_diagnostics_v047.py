from __future__ import annotations

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


SCHEMA = {
    "type": "object",
    "required": ["summary", "edits"],
    "properties": {
        "summary": {"type": "string"},
        "edits": {"type": "array"},
    },
}
PAYLOAD = {"instruction": "bounded offline unit test", "files": []}


def response_with(text: str, finish_reason: str | None = "STOP") -> dict:
    candidate = {"content": {"parts": [{"text": text}]}}
    if finish_reason is not None:
        candidate["finishReason"] = finish_reason
    return {
        "candidates": [candidate],
        "usageMetadata": {
            "promptTokenCount": 7,
            "candidatesTokenCount": 11,
        },
    }


class GeminiEngineeringResponseDiagnosticsV047Tests(unittest.TestCase):
    def _call(self, response: dict):
        model = GeminiEngineeringModel("unit-test-key")
        with patch.object(gemini_engineering, "request_json", return_value=response):
            return model.generate_patch(PAYLOAD, SCHEMA)

    def _error(self, response: dict, code: str, request_count: int = 1):
        with self.assertRaises(ProviderError) as caught:
            self._call(response)
        exc = caught.exception
        self.assertEqual(exc.code, code)
        self.assertTrue(exc.request_sent)
        self.assertEqual(exc.request_count, request_count)
        return exc

    def test_missing_candidates_is_explicit(self):
        self._error({}, "missing_candidates")
        self._error({"candidates": []}, "missing_candidates")

    def test_invalid_candidate_shape_is_explicit(self):
        self._error({"candidates": "not-a-list"}, "invalid_candidate_shape")
        self._error({"candidates": [None]}, "invalid_candidate_shape")

    def test_missing_content_and_parts_are_explicit(self):
        self._error({"candidates": [{"finishReason": "STOP"}]}, "missing_content")
        self._error(
            {"candidates": [{"finishReason": "STOP", "content": {}}]},
            "missing_parts",
        )
        self._error(
            {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]},
            "missing_parts",
        )

    def test_empty_response_text_is_explicit(self):
        self._error(
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [{}, {"text": ""}]}}]},
            "empty_response_text",
        )

    def test_malformed_json_is_explicit_without_raw_text(self):
        marker = "RAW_PROVIDER_TEXT_DO_NOT_PERSIST_47"
        exc = self._error(response_with('{"summary": "' + marker), "malformed_json")
        self.assertNotIn(marker, str(exc))
        self.assertNotIn(marker, repr(exc.__dict__))

    def test_non_object_json_is_explicit(self):
        self._error(response_with("[]"), "json_not_object")

    def test_max_tokens_is_output_truncated(self):
        self._error(
            {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "{}"}]}}]},
            "output_truncated",
        )

    def test_blocked_response_is_bounded(self):
        self._error(
            {"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]},
            "response_blocked",
        )
        self._error(
            {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []},
            "response_blocked",
        )

    def test_known_finish_reasons_are_sanitized_codes(self):
        cases = {
            "RECITATION": "response_recitation",
            "LANGUAGE": "unsupported_language",
            "OTHER": "generation_stopped_other",
            "MALFORMED_FUNCTION_CALL": "malformed_function_call",
        }
        for reason, code in cases.items():
            with self.subTest(reason=reason):
                self._error(
                    {"candidates": [{"finishReason": reason, "content": {"parts": []}}]},
                    code,
                )

    def test_unknown_finish_reason_is_bounded_not_raw(self):
        raw = "UNKNOWN_PROVIDER_REASON_secret_payload_47"
        exc = self._error(
            {"candidates": [{"finishReason": raw, "content": {"parts": []}}]},
            "generation_stopped_unknown",
        )
        self.assertNotIn(raw, str(exc))
        self.assertNotIn(raw, repr(exc.__dict__))

    def test_invalid_usage_metadata_is_sanitized(self):
        self._error(
            {
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"summary":"ok","edits":[]}'}]}}],
                "usageMetadata": "raw-usage-shape",
            },
            "invalid_usage_metadata",
        )

    def test_valid_structured_response_behavior_is_preserved(self):
        batch = self._call(response_with('{"summary":"ok","edits":[]}'))
        self.assertEqual(batch.data, {"summary": "ok", "edits": []})
        self.assertEqual(batch.api_requests, 1)
        self.assertEqual(batch.attempt_count, 1)
        self.assertEqual(batch.input_tokens, 7)
        self.assertEqual(batch.output_tokens, 11)
        self.assertEqual(batch.retry_delays, ())

    def test_parser_failure_after_retry_preserves_actual_attempt_count(self):
        delays: list[float] = []
        model = GeminiEngineeringModel(
            "unit-test-key",
            max_attempts=2,
            sleep_fn=delays.append,
            jitter_fn=lambda _low, _high: 0.0,
        )
        first = ProviderError("http_503", True, request_count=1)
        second = response_with("{")
        with patch.object(gemini_engineering, "request_json", side_effect=[first, second]):
            with self.assertRaises(ProviderError) as caught:
                model.generate_patch(PAYLOAD, SCHEMA)
        self.assertEqual(caught.exception.code, "malformed_json")
        self.assertEqual(caught.exception.request_count, 2)
        self.assertEqual(len(delays), 1)


if __name__ == "__main__":
    unittest.main()
