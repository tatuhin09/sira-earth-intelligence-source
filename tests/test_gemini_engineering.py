from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.providers.gemini_engineering import (
    GeminiEngineeringModel,
)


class GeminiEngineeringTests(unittest.TestCase):
    def test_request_is_generic_structured_tool_free_and_key_not_in_body(self):
        seen = {}

        def fake_request_json(
            request,
            timeout,
            opener,
            *,
            max_bytes,
        ):
            seen["request"] = request
            body = json.loads(
                request.data.decode("utf-8")
            )
            seen["body"] = body
            return {
                "candidates": [{
                    "content": {
                        "parts": [{
                            "text": json.dumps({
                                "summary": "fixture",
                                "edits": [],
                                "needs_more_context":
                                    False,
                                "context_requests": [],
                            })
                        }]
                    }
                }],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 3,
                },
            }

        model = GeminiEngineeringModel(
            "fake-key",
            max_attempts=1,
        )
        with patch(
            "sira.providers.gemini_engineering.request_json",
            side_effect=fake_request_json,
        ):
            batch = model.generate_patch(
                {
                    "schema":
                        "sira.engineering_writer_context.v1",
                    "files": [],
                    "editable_path_policy": {
                        "candidate_only": True
                    },
                },
                {
                    "type": "object",
                    "properties": {},
                },
            )

        encoded = json.dumps(
            seen["body"],
            ensure_ascii=False,
        )
        instruction = seen["body"][
            "systemInstruction"
        ]["parts"][0]["text"]

        self.assertNotIn("fake-key", encoded)
        self.assertNotIn("tools", seen["body"])
        self.assertIn(
            "isolated engineering",
            instruction,
        )
        self.assertIn(
            "dependency manifests",
            instruction,
        )
        self.assertEqual(
            batch.data["summary"],
            "fixture",
        )
        self.assertEqual(
            batch.input_tokens,
            10,
        )
        self.assertEqual(
            batch.output_tokens,
            3,
        )


if __name__ == "__main__":
    unittest.main()
