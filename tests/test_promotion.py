from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.main = Path(self.tmp.name) / "main"
        (self.main / "src" / "sira").mkdir(parents=True)
        (self.main / "tests").mkdir()
        (self.main / "benchmarks").mkdir()
        (self.main / "docs").mkdir()
        (self.main / "src" / "sira" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
        for name, content in {
            "runtime.py": "PROTECTED = 'runtime'\n",
            "cli.py": "PROTECTED = 'cli'\n",
            "self_modification.py": "PROTECTED = 'boundary'\n",
            "evaluator1.py": "PROTECTED = 'e1'\n",
            "promotion.py": "PROTECTED = 'promotion'\n",
            "rollback.py": "PROTECTED = 'rollback'\n",
        }.items():
            (self.main / "src" / "sira" / name).write_text(content, encoding="utf-8")
        (self.main / "tests" / "test_feature.py").write_text("# test\n", encoding="utf-8")
        (self.main / "benchmarks" / "case.json").write_text("{}\n", encoding="utf-8")
        (self.main / "docs" / "note.md").write_text("note\n", encoding="utf-8")
        (self.main / "sira.py").write_text("print('sira')\n", encoding="utf-8")
        (self.main / "README.md").write_text("readme\n", encoding="utf-8")
        (self.main / ".env.example").write_text("SAFE=1\n", encoding="utf-8")

        from sira.self_modification import prepare_candidate_workspace

        self.candidate = Path(self.tmp.name) / "candidate"
        prepare_candidate_workspace(self.main, self.candidate)
        (self.candidate / "src" / "sira" / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def health(test_count=151, passed_cases=4, passed=True):
        return {
            "tests": {"passed": passed, "test_count": test_count, "returncode": 0 if passed else 1},
            "benchmark": {
                "passed": passed,
                "passed_cases": passed_cases if passed else 0,
                "failed_cases": 0 if passed else 1,
                "returncode": 0 if passed else 1,
            },
            "overall_passed": passed,
        }

    def authorization_chain(self):
        from sira.evaluator1 import evaluate_final_gate
        from sira.evaluator2 import evaluate_candidate

        baseline = self.health()
        candidate_health = self.health(test_count=152)
        evaluator2 = evaluate_candidate(self.main, self.candidate, baseline, candidate_health)
        evaluator1 = evaluate_final_gate(
            self.main,
            self.candidate,
            evaluator2,
            baseline,
            candidate_health,
        )
        self.assertEqual(evaluator2["decision_code"], "candidate_verified")
        self.assertEqual(evaluator1["decision_code"], "promotion_authorized")
        return evaluator1, evaluator2

    def test_authorized_candidate_promotes_and_persists_audit_without_touching_protected_shell(self):
        from sira.promotion import promote_candidate

        evaluator1, evaluator2 = self.authorization_chain()
        protected_before = (self.main / "src" / "sira" / "runtime.py").read_bytes()
        calls = []

        def verifier(root):
            calls.append(root)
            return self.health(test_count=152)

        report = promote_candidate(
            self.main,
            self.candidate,
            evaluator1,
            evaluator2,
            verifier,
        )

        self.assertEqual(report["status"], "promoted")
        self.assertEqual(report["decision_code"], "promotion_committed")
        self.assertTrue(report["promotion_performed"])
        self.assertFalse(report["rollback_performed"])
        self.assertEqual(report["changed_files"], ["src/sira/feature.py"])
        self.assertEqual((self.main / "src" / "sira" / "feature.py").read_text(), "VALUE = 2\n")
        self.assertEqual((self.main / "src" / "sira" / "runtime.py").read_bytes(), protected_before)
        self.assertEqual(calls, [self.main.resolve()])
        self.assertTrue(Path(report["audit_path"]).is_file())

    def test_failed_post_promotion_verification_rolls_back_original_content(self):
        from sira.promotion import promote_candidate

        evaluator1, evaluator2 = self.authorization_chain()
        report = promote_candidate(
            self.main,
            self.candidate,
            evaluator1,
            evaluator2,
            lambda _root: self.health(passed=False),
        )

        self.assertEqual(report["status"], "rolled_back")
        self.assertEqual(report["decision_code"], "post_verification_failed")
        self.assertFalse(report["promotion_performed"])
        self.assertTrue(report["rollback_performed"])
        self.assertEqual((self.main / "src" / "sira" / "feature.py").read_text(), "VALUE = 1\n")
        self.assertTrue(report["rollback_verified"])

    def test_verifier_exception_rolls_back_new_file_as_well_as_modified_file(self):
        from sira.evaluator1 import evaluate_final_gate
        from sira.evaluator2 import evaluate_candidate
        from sira.promotion import promote_candidate

        (self.candidate / "src" / "sira" / "new_feature.py").write_text("NEW = True\n", encoding="utf-8")
        baseline = self.health()
        candidate_health = self.health(test_count=153)
        evaluator2 = evaluate_candidate(self.main, self.candidate, baseline, candidate_health)
        evaluator1 = evaluate_final_gate(self.main, self.candidate, evaluator2, baseline, candidate_health)

        def explode(_root):
            raise RuntimeError("verification exploded")

        report = promote_candidate(self.main, self.candidate, evaluator1, evaluator2, explode)
        self.assertEqual(report["status"], "rolled_back")
        self.assertEqual(report["decision_code"], "post_verification_error")
        self.assertFalse((self.main / "src" / "sira" / "new_feature.py").exists())
        self.assertEqual((self.main / "src" / "sira" / "feature.py").read_text(), "VALUE = 1\n")
        self.assertTrue(report["rollback_verified"])

    def test_stale_main_or_tampered_evaluator2_is_denied_before_any_write(self):
        from sira.promotion import promote_candidate

        evaluator1, evaluator2 = self.authorization_chain()
        before = (self.main / "src" / "sira" / "feature.py").read_text()
        (self.main / "docs" / "note.md").write_text("main changed after authorization\n", encoding="utf-8")
        report = promote_candidate(
            self.main,
            self.candidate,
            evaluator1,
            evaluator2,
            lambda _root: self.health(),
        )
        self.assertEqual(report["status"], "denied")
        self.assertEqual(report["decision_code"], "stale_authorization")
        self.assertEqual((self.main / "src" / "sira" / "feature.py").read_text(), before)

        # Fresh chain, then mutate the bound Evaluator-2 report.
        self.main.joinpath("docs/note.md").write_text("note\n", encoding="utf-8")
        evaluator1, evaluator2 = self.authorization_chain()
        evaluator2 = dict(evaluator2)
        evaluator2["decision_code"] = "tampered"
        report = promote_candidate(
            self.main,
            self.candidate,
            evaluator1,
            evaluator2,
            lambda _root: self.health(),
        )
        self.assertEqual(report["status"], "denied")
        self.assertEqual(report["decision_code"], "authorization_mismatch")
        self.assertEqual((self.main / "src" / "sira" / "feature.py").read_text(), before)


if __name__ == "__main__":
    unittest.main()
