from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [x for x in sys.path if x != str(SRC)]

from sira.providers.gemini_language import GeminiLanguageModel


class GeminiLanguageModelTests(unittest.TestCase):
    def test_interpret_is_structured_tool_free_and_key_not_in_body(self):
        response = {
            "candidates": [{"content": {"parts": [{"text": json.dumps({
                "detected_language": "Banglish",
                "canonical_english": "I will do it.",
                "research_queries": ["do it"],
                "candidate_mappings": [],
                "uncertainty": "low",
            })}]}}],
            "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 5},
        }
        sentinel = "gemini-language-key-sentinel"
        with patch("sira.providers.gemini_language.request_json", return_value=response) as mocked:
            batch = GeminiLanguageModel(sentinel).interpret(
                "ami korbo",
                {"language_label": "bn-Latn-banglish"},
            )
        request = mocked.call_args.args[0]
        body = json.loads(request.data)
        self.assertNotIn(sentinel, request.data.decode())
        self.assertNotIn("tools", body)
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertIn("responseJsonSchema", body["generationConfig"])
        self.assertEqual(batch.api_requests, 1)
        self.assertEqual(batch.input_tokens, 9)

    def test_verify_uses_distinct_verifier_instruction_and_no_tools(self):
        response = {
            "candidates": [{"content": {"parts": [{"text": json.dumps({
                "accepted": True,
                "semantic_equivalent": True,
                "intent_preserved": True,
                "verified_canonical_english": "I will do it.",
                "verified_research_queries": ["do it"],
                "mapping_verdicts": [],
                "reason": "verified",
            })}]}}],
        }
        with patch("sira.providers.gemini_language.request_json", return_value=response) as mocked:
            GeminiLanguageModel("language-key").verify(
                "ami korbo",
                {"canonical_english": "I will do it."},
            )
        body = json.loads(mocked.call_args.args[0].data)
        instruction = body["systemInstruction"]["parts"][0]["text"]
        self.assertIn("independent semantic verifier", instruction)
        self.assertNotIn("tools", body)


if __name__ == "__main__":
    unittest.main()
