from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.code_writer import CodeModelBatch
from sira.engineering_writer import (
    EngineeringResearchBackedWriter,
    EngineeringWriterOutputError,
    prepare_verified_engineering_candidate,
)
from sira.engineering_runtime import run_engineering_runtime_handoff
from sira.runtime import RuntimeStateStore, run_unified_improvement_cycle


SOURCE = """class Demo:
    def target(self, value: int) -> int:
        if value > 0:
            return value
        return 0

    def untouched(self) -> str:
        return "ok"
"""


class InvalidSignatureModel:
    name = "fixture_engineering_model"
    model_id = "fixture-invalid-signature"

    def generate_patch(self, payload, schema):
        return CodeModelBatch(
            {
                "summary": "Invalid scoped response marker-do-not-persist",
                "edits": [{
                    "path": "src/demo.py",
                    "symbol": "target",
                    "content": (
                        "def target(self, value, extra):\n"
                        "    return value\n"
                        "# raw-marker-do-not-persist\n"
                    ),
                    "reason": "fixture",
                }],
            },
            api_requests=1,
            attempt_count=2,
            retry_delays=(1.0,),
            input_tokens=120,
            output_tokens=40,
        )


def task():
    return {
        "task_id": "output-recovery-v052",
        "instruction": "Reduce branches in the selected symbol.",
        "target_paths": ["src/demo.py"],
        "target": {
            "path": "src/demo.py",
            "symbol": "target",
        },
        "language_hint": "python",
        "diagnostics": [],
    }


class EngineeringOutputRecoveryV052Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "project"
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "demo.py").write_text(
            SOURCE,
            encoding="utf-8",
        )
        RuntimeStateStore(self.root).save({
            "desired_state": "on",
            "worker_state": "running",
            "pid": None,
            "started_at": "2026-09-22T00:00:00+00:00",
            "heartbeat_at": "2026-09-22T00:00:00+00:00",
            "generation": 1,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_writer_exposes_stable_reason_and_request_usage(self):
        writer = EngineeringResearchBackedWriter(
            self.root,
            InvalidSignatureModel(),
        )

        with self.assertRaises(EngineeringWriterOutputError) as caught:
            writer.propose_text_edits(task())

        self.assertEqual(
            caught.exception.code,
            "scoped_target_signature_must_remain_unchanged",
        )
        self.assertEqual(writer.last_report["api_requests"], 1)
        self.assertEqual(writer.last_report["attempt_count"], 2)
        self.assertEqual(writer.last_report["input_tokens"], 120)
        self.assertEqual(writer.last_report["output_tokens"], 40)

    def test_candidate_preparer_persists_recoverable_rejection(self):
        workspace = self.base / "workspace"
        result = prepare_verified_engineering_candidate(
            self.root,
            task(),
            workspace,
            EngineeringResearchBackedWriter(
                self.root,
                InvalidSignatureModel(),
            ),
        )

        persisted = json.loads(
            (workspace / "engineering_writer_attempt.json").read_text(
                encoding="utf-8",
            )
        )
        self.assertEqual(result, persisted)
        self.assertEqual(result["status"], "model_output_rejected")
        self.assertEqual(
            result["writer_report"]["error_code"],
            "scoped_target_signature_must_remain_unchanged",
        )
        self.assertEqual(result["writer_report"]["api_requests"], 1)
        self.assertIsNone(result["candidate_root"])
        self.assertFalse(result["verification_executed"])
        self.assertFalse(result["promotion_authorized"])
        self.assertEqual(
            (self.root / "src" / "demo.py").read_text(encoding="utf-8"),
            SOURCE,
        )
        rendered = repr(result)
        self.assertNotIn("raw-marker-do-not-persist", rendered)
        self.assertNotIn("Invalid scoped response", rendered)

    def test_runtime_handoff_returns_rejection_without_evaluation(self):
        opportunity_id = "op_" + "a" * 32
        target = {
            "target_kind": "opportunity",
            "opportunity_id": opportunity_id,
            "path": "src/demo.py",
        }
        evidence = {
            "opportunity": {
                "opportunity_id": opportunity_id,
                "path": "src/demo.py",
                "symbol": "target",
                "summary": "Reduce branches in target.",
            },
            "assessment": {"decision": "research_ready"},
            "success_criteria": {"unit_tests_must_pass": True},
        }
        research = {
            "research_id": "or_" + "b" * 32,
            "writer_handoff_allowed": True,
            "research_quality": {"decision": "writer_ready"},
            "paid_spending": False,
            "metered_model_requests": 0,
        }

        def unexpected_evaluator(*_args, **_kwargs):
            raise AssertionError("rejected output must not reach evaluator")

        result = run_engineering_runtime_handoff(
            self.root,
            target,
            evidence,
            research,
            worker_task_id="task-output-recovery",
            writer_factory=lambda root: EngineeringResearchBackedWriter(
                root,
                InvalidSignatureModel(),
            ),
            evaluator=unexpected_evaluator,
            state_root=self.base / "runtime-state",
        )

        self.assertEqual(result["status"], "model_output_rejected")
        self.assertEqual(result["outcome"], "model_output_rejected")
        self.assertEqual(result["writer_report"]["api_requests"], 1)
        self.assertFalse(result["promotion_performed"])
        self.assertFalse(result["main_tree_modified"])
        self.assertTrue(Path(result["artifact"]).is_file())

    def test_unified_cycle_completes_instead_of_code_worker_failed(self):
        opportunity_id = "op_" + "c" * 32

        def selection(_root):
            return {
                "selection_id": "selection-output-recovery",
                "status": "selected",
                "target": {
                    "target_kind": "opportunity",
                    "opportunity_id": opportunity_id,
                    "path": "src/demo.py",
                },
                "alternatives": [],
            }

        def evidence(_root, selected_id):
            self.assertEqual(selected_id, opportunity_id)
            return {
                "evidence_id": "oe_" + "d" * 32,
                "opportunity": {
                    "opportunity_id": opportunity_id,
                    "fingerprint": "e" * 64,
                    "type": "complex_function",
                    "path": "src/demo.py",
                    "symbol": "target",
                    "summary": "Reduce branches in target.",
                },
                "assessment": {"decision": "research_ready"},
                "success_criteria": {"unit_tests_must_pass": True},
            }

        def research(_root, evidence_id):
            self.assertTrue(evidence_id.startswith("oe_"))
            return {
                "research_id": "or_" + "f" * 32,
                "opportunity_id": opportunity_id,
                "writer_handoff_allowed": True,
                "research_quality": {"decision": "writer_ready"},
                "paid_spending": False,
                "metered_model_requests": 0,
                "api_requests": 0,
            }

        def handoff(root, target, evidence_row, research_row, **kwargs):
            return run_engineering_runtime_handoff(
                root,
                target,
                evidence_row,
                research_row,
                writer_factory=lambda project: EngineeringResearchBackedWriter(
                    project,
                    InvalidSignatureModel(),
                ),
                state_root=self.base / "cycle-runtime-state",
                **kwargs,
            )

        result = run_unified_improvement_cycle(
            self.root,
            1,
            target_selector=selection,
            evidence_builder=evidence,
            opportunity_researcher=research,
            opportunity_verifier=lambda *_args: {"status": "verified"},
            opportunity_handoff_runner=lambda *_args, **_kwargs: (
                self.fail("legacy handoff must not run")
            ),
            engineering_handoff_runner=handoff,
            metered_budget_checker=lambda *_args, **_kwargs: {
                "allowed": True,
                "reason": "available",
                "retry_after_seconds": 0,
            },
            metered_attempt_recorder=lambda *_args, **_kwargs: {
                "recorded": True,
            },
            code_capability_preflight=lambda _root: {
                "status": "ready",
                "selected_provider_id": "gemini",
            },
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcome"], "model_output_rejected")
        self.assertEqual(
            result["resource_usage"]["metered_model_requests"],
            1,
        )
        coordination = result["worker_coordination"]
        self.assertEqual(coordination["worker_states"]["code"], "completed")
        self.assertEqual(coordination["worker_failures"], {})


if __name__ == "__main__":
    unittest.main()
