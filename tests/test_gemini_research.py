from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path[:] = [str(SRC)] + [item for item in sys.path if item != str(SRC)]

from sira.providers.gemini_research import (
    MODE_COMBINED,
    MODE_GOOGLE_SEARCH,
    MODE_URL_CONTEXT,
    GeminiResearchModel,
)


class GeminiResearchModelTests(unittest.TestCase):
    def test_url_context_request_uses_only_url_tool_and_parses_metadata(self):
        response = {
            "candidates": [{
                "content": {"parts": [{"text": "Result"}]},
                "urlContextMetadata": {
                    "urlMetadata": [{
                        "retrievedUrl": "https://example.org/doc",
                        "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_SUCCESS",
                    }]
                },
            }],
            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7},
        }
        with patch("sira.providers.gemini_research.request_json", return_value=response) as mocked:
            batch = GeminiResearchModel("actual-gemini-key-sentinel").research(
                "Analyze this source",
                urls=("https://example.org/doc",),
                mode=MODE_URL_CONTEXT,
            )

        request = mocked.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["tools"], [{"url_context": {}}])
        self.assertNotIn("actual-gemini-key-sentinel", request.data.decode())
        self.assertEqual(batch.url_retrievals[0]["url"], "https://example.org/doc")
        self.assertEqual(batch.input_tokens, 11)
        self.assertEqual(batch.output_tokens, 7)

    def test_google_search_parses_queries_sources_and_supports(self):
        response = {
            "candidates": [{
                "content": {"parts": [{"text": "Grounded answer"}]},
                "groundingMetadata": {
                    "webSearchQueries": ["current documentation"],
                    "groundingChunks": [{
                        "web": {"uri": "https://example.org/source", "title": "Source"}
                    }],
                    "groundingSupports": [{
                        "segment": {"startIndex": 0, "endIndex": 8},
                        "groundingChunkIndices": [0],
                    }],
                },
            }]
        }
        with patch("sira.providers.gemini_research.request_json", return_value=response) as mocked:
            batch = GeminiResearchModel("actual-gemini-key-sentinel").research(
                "Find current docs",
                mode=MODE_GOOGLE_SEARCH,
            )
        body = json.loads(mocked.call_args.args[0].data)
        self.assertEqual(body["tools"], [{"google_search": {}}])
        self.assertEqual(batch.web_search_queries, ("current documentation",))
        self.assertEqual(batch.sources[0].url, "https://example.org/source")
        self.assertEqual(batch.grounding_supports[0]["grounding_chunk_indices"], [0])

    def test_combined_mode_enables_both_official_tools(self):
        response = {
            "candidates": [{
                "content": {"parts": [{"text": "Combined"}]},
                "groundingMetadata": {},
                "urlContextMetadata": {"urlMetadata": []},
            }]
        }
        with patch("sira.providers.gemini_research.request_json", return_value=response) as mocked:
            GeminiResearchModel("actual-gemini-key-sentinel").research(
                "Compare and verify",
                urls=("https://example.org/doc",),
                mode=MODE_COMBINED,
            )
        tools = json.loads(mocked.call_args.args[0].data)["tools"]
        self.assertEqual(tools, [{"url_context": {}}, {"google_search": {}}])

    def test_private_url_and_invalid_mode_fail_before_network(self):
        model = GeminiResearchModel("secret")
        with patch("sira.providers.gemini_research.request_json") as mocked:
            with self.assertRaises(ValueError):
                model.research(
                    "read",
                    urls=("http://127.0.0.1/private",),
                    mode=MODE_URL_CONTEXT,
                )
            with self.assertRaises(ValueError):
                model.research("read", mode="unknown")
        mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
