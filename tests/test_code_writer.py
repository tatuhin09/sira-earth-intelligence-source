import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class _FixtureCodeModel:
    name = "fixture_code_model"
    model_id = "fixture-v1"

    def __init__(self, data):
        self.data = data
        self.calls = []

    def generate_patch(self, payload, schema):
        from sira.code_writer import CodeModelBatch
        self.calls.append((payload, schema))
        return CodeModelBatch(self.data, api_requests=1, input_tokens=123, output_tokens=45)


class CodeWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "main"
        (self.root / "src" / "sira" / "providers").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "benchmarks").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "src" / "sira" / "papers.py").write_text(
            "def search_papers():\n    return 'old'\n", encoding="utf-8")
        (self.root / "src" / "sira" / "providers" / "semantic_scholar.py").write_text(
            "RATE_LIMIT = True\n", encoding="utf-8")
        (self.root / "src" / "sira" / "runtime.py").write_text(
            "PROTECTED_SECRET = 'do-not-send'\n", encoding="utf-8")
        (self.root / "src" / "sira" / "self_modification.py").write_text(
            "BOUNDARY = True\n", encoding="utf-8")
        (self.root / "tests" / "test_papers.py").write_text("# paper tests\n", encoding="utf-8")
        (self.root / "benchmarks" / "paper_cases.json").write_text("{}\n", encoding="utf-8")
        (self.root / "docs" / "papers.md").write_text("Semantic Scholar fallback notes\n", encoding="utf-8")
        (self.root / "sira.py").write_text("print('sira')\n", encoding="utf-8")
        (self.root / "README.md").write_text("readme\n", encoding="utf-8")
        (self.root / ".env.example").write_text("GEMINI_API_KEY=\n", encoding="utf-8")
        (self.root / ".env").write_text("GEMINI_API_KEY=real-secret\n", encoding="utf-8")
        (self.root / "src" / "sira" / "providers" / "linked.py").symlink_to(
            self.root / "src" / "sira" / "runtime.py")

    def tearDown(self):
        self.tmp.cleanup()

    def hypothesis(self):
        return {
            "hypothesis_id": "ih_" + "a" * 32,
            "memory_id": "m_" + "b" * 32,
            "benchmark_suite": "papers",
            "statement": "For scholarly_research, reduce Semantic Scholar rate-limit failures using existing fallback paths.",
            "rationale": "Preserve bounded retry and independent fallback providers.",
            "memory_snapshot": {
                "capability": "scholarly_research",
                "provider": "semantic_scholar",
                "error_code": "rate_limited",
                "summary": "semantic_scholar rate limited failure in scholarly_research",
            },
            "related_memories": [],
        }

    def test_project_context_is_relevant_bounded_and_excludes_protected_or_secret_files(self):
        from sira.code_writer import build_project_context

        context = build_project_context(self.root, self.hypothesis())
        paths = [row["path"] for row in context["files"]]

        self.assertIn("src/sira/providers/semantic_scholar.py", paths)
        self.assertIn("src/sira/papers.py", paths)
        self.assertIn("tests/test_papers.py", paths)
        self.assertNotIn("src/sira/runtime.py", paths)
        self.assertNotIn("src/sira/providers/linked.py", paths)
        self.assertNotIn(".env", paths)
        self.assertEqual(len(context["context_sha256"]), 64)
        wire = json.dumps(context, ensure_ascii=False)
        self.assertNotIn("real-secret", wire)
        self.assertNotIn("do-not-send", wire)

    def test_structured_model_patch_is_validated_and_applied_only_to_isolated_candidate(self):
        from sira.code_writer import ResearchBackedCodeWriter, prepare_code_candidate

        new_text = "def search_papers():\n    return 'bounded-fallback'\n"
        model = _FixtureCodeModel({
            "summary": "Use the existing bounded fallback path.",
            "edits": [{
                "path": "src/sira/papers.py",
                "content": new_text,
                "reason": "Make fallback selection explicit.",
            }],
        })
        writer = ResearchBackedCodeWriter(self.root, model)
        workspace = self.root / "scratch" / "writer-attempt"
        result = prepare_code_candidate(self.root, self.hypothesis(), workspace, writer)

        self.assertEqual(result["status"], "candidate_prepared")
        self.assertEqual(result["api_requests"], 1)
        self.assertEqual(result["input_tokens"], 123)
        self.assertEqual(result["output_tokens"], 45)
        self.assertFalse(result["main_tree_modified"])
        self.assertFalse(result["promotion_performed"])
        candidate = Path(result["candidate_root"])
        self.assertEqual((candidate / "src" / "sira" / "papers.py").read_text(), new_text)
        self.assertNotEqual((self.root / "src" / "sira" / "papers.py").read_text(), new_text)
        self.assertTrue((workspace / "code_writer_attempt.json").is_file())
        self.assertEqual(len(model.calls), 1)

    def test_invalid_duplicate_or_unexpected_model_output_is_rejected_before_candidate_write(self):
        from sira.code_writer import CodeWriterOutputError, ResearchBackedCodeWriter

        bad_payloads = [
            {"summary": "x", "edits": [
                {"path": "src/sira/papers.py", "content": "A\n", "reason": "x"},
                {"path": "src/sira/papers.py", "content": "B\n", "reason": "y"},
            ]},
            {"summary": "x", "edits": [], "unexpected": True},
            {"summary": "x", "edits": [{"path": "src/sira/papers.py", "content": "A\n"}]},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                writer = ResearchBackedCodeWriter(self.root, _FixtureCodeModel(payload))
                with self.assertRaises(CodeWriterOutputError):
                    writer.propose_text_edits(self.hypothesis())

    def test_protected_model_edit_is_rejected_by_candidate_boundary_and_main_stays_unchanged(self):
        from sira.code_writer import ResearchBackedCodeWriter, prepare_code_candidate
        from sira.self_modification import CandidateEditError

        original = (self.root / "src" / "sira" / "runtime.py").read_text()
        model = _FixtureCodeModel({
            "summary": "bad protected edit",
            "edits": [{
                "path": "src/sira/runtime.py",
                "content": "PROTECTED_SECRET = 'changed'\n",
                "reason": "not allowed",
            }],
        })
        writer = ResearchBackedCodeWriter(self.root, model)
        with self.assertRaises(CandidateEditError):
            prepare_code_candidate(self.root, self.hypothesis(), self.root / "scratch" / "blocked", writer)
        self.assertEqual((self.root / "src" / "sira" / "runtime.py").read_text(), original)

    def test_no_edit_response_is_a_non_mutating_result(self):
        from sira.code_writer import ResearchBackedCodeWriter, prepare_code_candidate

        writer = ResearchBackedCodeWriter(self.root, _FixtureCodeModel({
            "summary": "Existing implementation already matches the hypothesis.",
            "edits": [],
        }))
        workspace = self.root / "scratch" / "no-edit"
        result = prepare_code_candidate(self.root, self.hypothesis(), workspace, writer)

        self.assertEqual(result["status"], "no_edit_generated")
        self.assertFalse(result["main_tree_modified"])
        self.assertFalse(result["promotion_performed"])
        self.assertIsNone(result["candidate_root"])
        self.assertTrue((workspace / "code_writer_attempt.json").is_file())


class GeminiCodeModelTests(unittest.TestCase):
    def test_request_is_structured_bounded_and_has_no_tools_or_secret_in_body(self):
        from sira.code_writer import CODE_PATCH_SCHEMA
        from sira.providers.gemini_code import GeminiCodeModel

        response_payload = {
            "candidates": [{"content": {"parts": [{"text": json.dumps({
                "summary": "small change",
                "edits": [{"path": "src/sira/x.py", "content": "VALUE = 2\n", "reason": "fix"}],
            })}]}}],
            "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 9},
        }
        with patch("sira.providers.gemini_code.open_request", return_value=io.BytesIO(json.dumps(response_payload).encode())) as http:
            batch = GeminiCodeModel("fake-key").generate_patch({"hypothesis": {"statement": "fix x"}, "files": []}, CODE_PATCH_SCHEMA)

        request = http.call_args.args[0]
        body = json.loads(request.data)
        self.assertIn("gemini-3.1-flash-lite:generateContent", request.full_url)
        self.assertEqual(request.headers["X-goog-api-key"], "fake-key")
        self.assertNotIn(b"fake-key", request.data)
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertNotIn("tools", body)
        self.assertIn("candidate code", body["systemInstruction"]["parts"][0]["text"].lower())
        self.assertEqual(batch.data["edits"][0]["path"], "src/sira/x.py")
        self.assertEqual(batch.input_tokens, 20)
        self.assertEqual(batch.output_tokens, 9)
        self.assertEqual(http.call_count, 1)


if __name__ == "__main__":
    unittest.main()

class GeminiCodeModelResilienceTests(unittest.TestCase):
    @staticmethod
    def _success_payload():
        return {
            "candidates": [{"content": {"parts": [{"text": json.dumps({
                "summary": "small change",
                "edits": [{"path": "src/sira/x.py", "content": "VALUE = 2\n", "reason": "fix"}],
            })}]}}],
            "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 9},
        }

    def test_transient_503_retries_with_bounded_backoff_and_jitter_then_succeeds(self):
        from sira.code_writer import CODE_PATCH_SCHEMA
        from sira.models import ProviderError
        from sira.providers.gemini_code import GeminiCodeModel

        sleeps = []
        model = GeminiCodeModel(
            "fake-key",
            max_attempts=3,
            sleep_fn=sleeps.append,
            jitter_fn=lambda low, high: 0.1,
        )
        with patch(
            "sira.providers.gemini_code.request_json",
            side_effect=[ProviderError("http_503", True), self._success_payload()],
        ) as request_json_mock:
            batch = model.generate_patch({"hypothesis": {"statement": "fix x"}, "files": []}, CODE_PATCH_SCHEMA)

        self.assertEqual(request_json_mock.call_count, 2)
        self.assertEqual(batch.api_requests, 2)
        self.assertEqual(batch.attempt_count, 2)
        self.assertEqual(batch.retry_delays, (0.6,))
        self.assertEqual(sleeps, [0.6])

    def test_retryable_provider_errors_are_bounded_and_final_error_reports_total_attempts(self):
        from sira.code_writer import CODE_PATCH_SCHEMA
        from sira.models import ProviderError
        from sira.providers.gemini_code import GeminiCodeModel

        for code in ("http_429", "http_502", "http_503", "http_504", "network_or_timeout"):
            with self.subTest(code=code):
                sleeps = []
                model = GeminiCodeModel(
                    "fake-key",
                    max_attempts=3,
                    sleep_fn=sleeps.append,
                    jitter_fn=lambda low, high: 0.0,
                )
                with patch(
                    "sira.providers.gemini_code.request_json",
                    side_effect=ProviderError(code, True),
                ) as request_json_mock:
                    with self.assertRaises(ProviderError) as caught:
                        model.generate_patch({"hypothesis": {"statement": "fix x"}, "files": []}, CODE_PATCH_SCHEMA)

                self.assertEqual(request_json_mock.call_count, 3)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.request_count, 3)
                self.assertEqual(tuple(caught.exception.retry_delays), (0.5, 1.0))
                self.assertEqual(sleeps, [0.5, 1.0])

    def test_retry_after_is_bounded_and_nonretryable_auth_error_is_not_retried(self):
        from sira.code_writer import CODE_PATCH_SCHEMA
        from sira.models import ProviderError
        from sira.providers.gemini_code import GeminiCodeModel

        sleeps = []
        model = GeminiCodeModel(
            "fake-key",
            max_attempts=2,
            sleep_fn=sleeps.append,
            jitter_fn=lambda low, high: 0.0,
        )
        with patch(
            "sira.providers.gemini_code.request_json",
            side_effect=[ProviderError("http_429", True, retry_after=99), self._success_payload()],
        ):
            batch = model.generate_patch({"hypothesis": {"statement": "fix x"}, "files": []}, CODE_PATCH_SCHEMA)
        self.assertEqual(batch.retry_delays, (5.0,))
        self.assertEqual(sleeps, [5.0])

        with patch(
            "sira.providers.gemini_code.request_json",
            side_effect=ProviderError("http_401", True),
        ) as request_json_mock:
            with self.assertRaises(ProviderError) as caught:
                model.generate_patch({"hypothesis": {"statement": "fix x"}, "files": []}, CODE_PATCH_SCHEMA)
        self.assertEqual(request_json_mock.call_count, 1)
        self.assertEqual(caught.exception.code, "http_401")
        self.assertEqual(caught.exception.request_count, 1)

class SmartContextBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "main"
        (self.root / "src" / "sira" / "providers").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "benchmarks").mkdir()
        (self.root / "docs").mkdir()
        (self.root / "src" / "sira" / "papers.py").write_text(
            "def search_papers():\n    return 'old'\n" + ("# fallback research\n" * 200), encoding="utf-8")
        (self.root / "src" / "sira" / "providers" / "semantic_scholar.py").write_text(
            "RATE_LIMIT = True\n" + ("# semantic scholar rate limit fallback\n" * 250), encoding="utf-8")
        (self.root / "src" / "sira" / "providers" / "crossref.py").write_text(
            "FALLBACK = 'crossref'\n" + ("# fallback provider\n" * 200), encoding="utf-8")
        (self.root / "src" / "sira" / "providers" / "arxiv.py").write_text(
            "FALLBACK = 'arxiv'\n" + ("# fallback provider\n" * 200), encoding="utf-8")
        (self.root / "tests" / "test_papers.py").write_text(
            "# paper tests\n" + ("# rate limit fallback test\n" * 250), encoding="utf-8")
        (self.root / "benchmarks" / "paper_cases.json").write_text("{}\n", encoding="utf-8")
        (self.root / "docs" / "extra_notes.md").write_text("extra notes\n", encoding="utf-8")
        (self.root / "src" / "sira" / "runtime.py").write_text("PROTECTED = True\n", encoding="utf-8")
        (self.root / "src" / "sira" / "self_modification.py").write_text("BOUNDARY = True\n", encoding="utf-8")
        (self.root / "README.md").write_text("SIRA\n", encoding="utf-8")
        (self.root / ".env.example").write_text("GEMINI_API_KEY=\n", encoding="utf-8")
        (self.root / "sira.py").write_text("# entry\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def hypothesis(self):
        return {
            "hypothesis_id": "ih_" + "a" * 32,
            "memory_id": "m_" + "b" * 32,
            "benchmark_suite": "papers",
            "statement": "For scholarly_research, reduce Semantic Scholar rate-limit failures using existing fallback paths.",
            "rationale": "Preserve bounded retry and independent fallback providers.",
            "memory_snapshot": {
                "capability": "scholarly_research",
                "provider": "semantic_scholar",
                "error_code": "rate_limited",
                "summary": "semantic_scholar rate limited failure in scholarly_research",
            },
            "related_memories": [],
        }

    def test_primary_context_uses_small_budget_and_exposes_ranked_index(self):
        from sira.code_writer import (
            PRIMARY_CONTEXT_FILE_LIMIT,
            PRIMARY_CONTEXT_TOTAL_BYTES,
            build_project_context,
        )

        context = build_project_context(self.root, self.hypothesis())

        self.assertEqual(context["context_tier"], "primary")
        self.assertLessEqual(context["context_file_count"], PRIMARY_CONTEXT_FILE_LIMIT)
        self.assertLessEqual(context["context_bytes"], PRIMARY_CONTEXT_TOTAL_BYTES)
        self.assertGreater(len(context["available_context_index"]), 0)
        self.assertTrue(all("content" not in row for row in context["available_context_index"]))
        paths = [row["path"] for row in context["files"]]
        self.assertIn("src/sira/providers/semantic_scholar.py", paths)

    def test_explicit_new_file_task_does_not_send_unrelated_project_files(self):
        from sira.code_writer import build_project_context

        hypothesis = self.hypothesis()
        hypothesis["statement"] = (
            "Create exactly docs/writer_live_check_candidate.md with marker SIRA_WRITER_LIVE_CHECK_OK."
        )
        context = build_project_context(self.root, hypothesis)

        self.assertEqual(context["explicit_paths"], ["docs/writer_live_check_candidate.md"])
        self.assertEqual(context["files"], [])
        self.assertEqual(context["context_bytes"], 0)
        self.assertIn("docs/extra_notes.md", [row["path"] for row in context["available_context_index"]])

    def test_writer_performs_one_bounded_context_expansion_when_model_requests_file(self):
        from sira.code_writer import CodeModelBatch, ResearchBackedCodeWriter

        class ExpandingModel:
            name = "fixture_expand"
            model_id = "fixture-expand-v1"

            def __init__(self):
                self.calls = []

            def generate_patch(self, payload, schema):
                self.calls.append(payload)
                if len(self.calls) == 1:
                    return CodeModelBatch({
                        "summary": "Need the implementation file before editing.",
                        "edits": [],
                        "needs_more_context": True,
                        "context_requests": ["src/sira/papers.py"],
                    }, api_requests=1, input_tokens=100, output_tokens=10)
                return CodeModelBatch({
                    "summary": "Patch the implementation after reading it.",
                    "edits": [{
                        "path": "src/sira/papers.py",
                        "content": "def search_papers():\n    return 'smarter'\n",
                        "reason": "Use the researched fallback behavior.",
                    }],
                    "needs_more_context": False,
                    "context_requests": [],
                }, api_requests=1, input_tokens=80, output_tokens=20)

        model = ExpandingModel()
        writer = ResearchBackedCodeWriter(self.root, model)
        edits = writer.propose_text_edits(self.hypothesis())

        self.assertEqual(edits["src/sira/papers.py"], "def search_papers():\n    return 'smarter'\n")
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(model.calls[0]["context_tier"], "primary")
        self.assertEqual(model.calls[1]["context_tier"], "expanded")
        self.assertIn("src/sira/papers.py", [row["path"] for row in model.calls[1]["files"]])
        self.assertEqual(writer.last_report["context_pass_count"], 2)
        self.assertEqual(writer.last_report["api_requests"], 2)
        self.assertEqual(writer.last_report["input_tokens"], 180)
        self.assertGreaterEqual(writer.last_report["context_total_bytes"], writer.last_report["context_bytes"])

    def test_existing_edit_target_not_in_primary_context_triggers_safe_second_pass(self):
        from sira.code_writer import CodeModelBatch, ResearchBackedCodeWriter

        class ImplicitExpansionModel:
            name = "fixture_implicit_expand"
            model_id = "fixture-implicit-v1"

            def __init__(self):
                self.calls = []

            def generate_patch(self, payload, schema):
                self.calls.append(payload)
                if len(self.calls) == 1:
                    return CodeModelBatch({
                        "summary": "Edit supporting notes.",
                        "edits": [{
                            "path": "docs/extra_notes.md",
                            "content": "changed without seeing source\n",
                            "reason": "support docs",
                        }],
                    })
                return CodeModelBatch({
                    "summary": "Edit supporting notes after reading source.",
                    "edits": [{
                        "path": "docs/extra_notes.md",
                        "content": "extra notes\nupdated safely\n",
                        "reason": "support docs",
                    }],
                })

        model = ImplicitExpansionModel()
        writer = ResearchBackedCodeWriter(self.root, model)
        edits = writer.propose_text_edits(self.hypothesis())

        self.assertEqual(len(model.calls), 2)
        self.assertIn("docs/extra_notes.md", [row["path"] for row in model.calls[1]["files"]])
        self.assertEqual(edits["docs/extra_notes.md"], "extra notes\nupdated safely\n")

    def test_protected_context_request_is_rejected_before_second_model_call(self):
        from sira.code_writer import CodeModelBatch, CodeWriterOutputError, ResearchBackedCodeWriter

        class ProtectedRequestModel:
            name = "fixture_protected_request"
            model_id = "fixture-protected-v1"

            def __init__(self):
                self.calls = 0

            def generate_patch(self, payload, schema):
                self.calls += 1
                return CodeModelBatch({
                    "summary": "Need protected runtime.",
                    "edits": [],
                    "needs_more_context": True,
                    "context_requests": ["src/sira/runtime.py"],
                })

        model = ProtectedRequestModel()
        writer = ResearchBackedCodeWriter(self.root, model)
        with self.assertRaises(CodeWriterOutputError):
            writer.propose_text_edits(self.hypothesis())
        self.assertEqual(model.calls, 1)
