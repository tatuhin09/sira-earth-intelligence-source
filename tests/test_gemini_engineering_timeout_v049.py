from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import sira.providers.gemini_engineering as gemini_engineering
from sira.providers.gemini_engineering import GeminiEngineeringModel


class GeminiEngineeringTimeoutHeadroomV049Tests(unittest.TestCase):
    def test_default_timeout_is_sixty_seconds(self):
        model = GeminiEngineeringModel("unit-test-key")
        self.assertEqual(model.timeout, 60)
        self.assertEqual(model.max_attempts, 3)

    def test_request_uses_default_sixty_second_timeout_without_policy_changes(self):
        seen: list[int] = []

        def fake_request_json(request, timeout, opener, *, max_bytes):
            seen.append(timeout)
            return {
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "{}"}]},
                }],
                "usageMetadata": {
                    "promptTokenCount": 1,
                    "candidatesTokenCount": 1,
                },
            }

        model = GeminiEngineeringModel("unit-test-key")
        with patch.object(gemini_engineering, "request_json", side_effect=fake_request_json):
            batch = model.generate_patch({}, {"type": "object"})

        self.assertEqual(seen, [60])
        self.assertEqual(batch.attempt_count, 1)
        self.assertEqual(batch.api_requests, 1)

    def test_explicit_bounded_timeout_override_is_preserved(self):
        model = GeminiEngineeringModel("unit-test-key", timeout=17)
        self.assertEqual(model.timeout, 17)

    def test_timeout_validation_upper_bound_is_unchanged(self):
        with self.assertRaises(ValueError):
            GeminiEngineeringModel("unit-test-key", timeout=61)


if __name__ == "__main__":
    unittest.main()
