"""Read-only verification assets must survive both independent gates."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.engineering_authorization import _surface_snapshot, evaluate_engineering_authorization
from sira.engineering_editing import (
    _copy_digest, EngineeringCandidateEditError, ValidatedEngineeringCandidateEditor,
)
from sira.engineering_evaluator import evaluate_engineering_candidate
from sira.engineering_writer import EngineeringResearchBackedWriter, prepare_verified_engineering_candidate
from tests.test_engineering_evaluator import FixtureModel, passing_runner


SUPPORT = {
    ".gitignore": "runtime/\n",
    "README.md": "# Project\n",
    "benchmarks/cases.json": "{}\n",
    "desktop/static/index.html": "<main>App</main>\n",
    "desktop/static/styles.css": "main {}\n",
    "desktop/assets/sira.svg": "<svg/>\n",
    "fixtures/settings.toml": "enabled = true\n",
    "fixtures/settings.yaml": "enabled: true\n",
    "fixtures/settings.yml": "enabled: true\n",
    "fixtures/expected.txt": "expected\n",
}


class EngineeringSupportIntegrityV061Tests(unittest.TestCase):
    def make_attempt(self, base):
        root = base / "project"
        for relative, content in {
            **SUPPORT, "src/app.py": "def value():\n    return 1\n",
            "tests/test_app.py": "from src.app import value\n",
        }.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        attempt = prepare_verified_engineering_candidate(
            root, {"task_id": "support-parity", "instruction": "Return 2 from value.",
                   "target_paths": ["src/app.py"]}, base / "writer",
            EngineeringResearchBackedWriter(root, FixtureModel()), command_runner=passing_runner,
        )
        return root, attempt

    def test_unchanged_support_passes_evaluator_and_checksum_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, attempt = self.make_attempt(Path(tmp))
            self.assertEqual(_copy_digest(root)[0], _surface_snapshot(root)[0])
            evaluation = evaluate_engineering_candidate(root, attempt)
            self.assertEqual(evaluation["decision"], "accept", evaluation["risk_flags"])
            authorization = evaluate_engineering_authorization(root, attempt, evaluation)
            self.assertEqual(authorization["decision"], "allow", authorization.get("failures"))
            self.assertTrue(authorization["authorization"]["checksum_only"])
            self.assertFalse(authorization["authorization"]["write_authority"])
            self.assertFalse(authorization["authorization"]["promotion_execution_authority"])

    def test_changed_added_or_deleted_support_is_rejected_by_both_gates(self):
        for mode in ("change", "add", "delete"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root, attempt = self.make_attempt(Path(tmp))
                approved_evaluation = evaluate_engineering_candidate(root, attempt)
                candidate = Path(attempt["candidate_root"])
                target = candidate / "benchmarks/cases.json"
                if mode == "change":
                    target.write_text('{"weakened":true}')
                elif mode == "add":
                    (candidate / "fixtures/extra.json").write_text("{}")
                else:
                    target.unlink()
                evaluation = evaluate_engineering_candidate(root, attempt)
                self.assertEqual(evaluation["decision"], "reject")
                self.assertIn("changed_set_mismatch", evaluation["risk_flags"])
                # Even a previously accepted evaluator report cannot authorize tampering.
                authorization = evaluate_engineering_authorization(root, attempt, approved_evaluation)
                self.assertNotEqual(authorization["decision"], "allow")

    def test_main_support_change_invalidates_candidate_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, attempt = self.make_attempt(Path(tmp))
            evaluation = evaluate_engineering_candidate(root, attempt)
            (root / "README.md").write_text("# New baseline\n")
            self.assertEqual(evaluate_engineering_candidate(root, attempt)["decision_code"], "stale_candidate_base")
            authorization = evaluate_engineering_authorization(root, attempt, evaluation)
            self.assertNotEqual(authorization["decision"], "allow")
            self.assertIn("stale_candidate_base", authorization["failures"])

    def test_support_remains_non_editable_and_secrets_remain_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, attempt = self.make_attempt(Path(tmp))
            candidate = Path(attempt["candidate_root"])
            editor = ValidatedEngineeringCandidateEditor(attempt["edit_candidate"]["policy"])
            for relative in SUPPORT:
                with self.subTest(path=relative), self.assertRaises(EngineeringCandidateEditError):
                    editor.apply_text_edits(candidate, {relative: "changed"})
            (candidate / "credentials.json").write_text('{"token":"fixture"}')
            result = evaluate_engineering_candidate(root, attempt)
            self.assertIn("sensitive_artifact_present", result["risk_flags"])


if __name__ == "__main__":
    unittest.main()
