from pathlib import Path
import hashlib
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.engineering_verification import execute_verification_plan, verification_execution_contract


def plan(*, command_id="go.test", available=True, argv=None, env=None):
    return {
        "schema": "sira.engineering_verification_plan.v1",
        "candidate_only_required": True,
        "network_disabled_required": True,
        "execution_performed": False,
        "commands": [{
            "command_id": command_id,
            "kind": "test",
            "language": "go",
            "argv": argv or ["go", "test", "./..."],
            "cwd": ".",
            "env": env if env is not None else {"GOPROXY": "off", "GOSUMDB": "off"},
            "available": available,
            "candidate_only": True,
            "network_policy": "sandbox_network_must_be_disabled",
            "writes_generated_artifacts": True,
            "shell": False,
            "authority_granted": False,
            "promotion_authorized": False,
        }],
    }


class EngineeringVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.main = self.base / "main"
        self.candidate = self.base / "candidate"
        self.main.mkdir()
        self.candidate.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def runner(status="passed"):
        def run(argv, *, cwd, timeout_seconds, max_output_bytes):
            payload = b"aggregate-output"
            return {
                "status": status,
                "returncode": 0 if status == "passed" else 1,
                "timed_out": status == "timeout",
                "output_limit_exceeded": status == "output_limit",
                "output_bytes": len(payload),
                "output_sha256": hashlib.sha256(payload).hexdigest(),
                "duration_ms": 2,
            }
        return run

    def test_success_is_candidate_only_aggregate_and_non_authoritative(self):
        result = execute_verification_plan(
            self.candidate,
            plan(),
            main_root=self.main,
            command_runner=self.runner(),
        )
        self.assertTrue(result["overall_passed"])
        self.assertEqual(result["outcome"], "verification_passed")
        self.assertEqual(result["processes_executed"], 1)
        self.assertFalse(result["raw_output_included"])
        self.assertFalse(result["promotion_authorized"])
        self.assertFalse(result["main_tree_modified"])
        self.assertFalse(result["package_installation_performed"])

    def test_missing_tool_and_missing_isolation_fail_closed(self):
        missing = execute_verification_plan(
            self.candidate, plan(available=False), command_runner=self.runner()
        )
        self.assertEqual(missing["outcome"], "required_tool_unavailable")
        self.assertEqual(missing["processes_executed"], 0)

        blocked = execute_verification_plan(
            self.candidate, plan(), bubblewrap_path=""
        )
        self.assertEqual(
            blocked["outcome"],
            "network_filesystem_isolation_unavailable",
        )
        self.assertEqual(blocked["processes_executed"], 0)

    def test_timeout_output_limit_and_command_failure_are_failures(self):
        expected = {
            "timeout": "verification_timeout",
            "output_limit": "verification_output_limit",
            "failed": "verification_failed",
        }
        for status, outcome in expected.items():
            with self.subTest(status=status):
                result = execute_verification_plan(
                    self.candidate, plan(), command_runner=self.runner(status)
                )
                self.assertFalse(result["overall_passed"])
                self.assertEqual(result["outcome"], outcome)

    def test_unsafe_or_dependency_mutating_plans_are_rejected(self):
        bad_rows = [
            plan(argv=["sudo", "go", "test", "./..."]),
            plan(argv=["go", "env"]),
            plan(env={"SECRET": "value"}),
        ]
        node = plan(command_id="node.test", argv=["npm", "install"], env={})
        node["commands"][0]["language"] = "typescript"
        bad_rows.append(node)
        for value in bad_rows:
            with self.subTest(value=value["commands"][0]["argv"]):
                with self.assertRaises(ValueError):
                    execute_verification_plan(
                        self.candidate, value, command_runner=self.runner()
                    )

    def test_main_tree_and_nested_main_workspace_are_rejected(self):
        with self.assertRaises(ValueError):
            execute_verification_plan(
                self.main, plan(), main_root=self.main, command_runner=self.runner()
            )
        nested = self.main / "candidate"
        nested.mkdir()
        with self.assertRaises(ValueError):
            execute_verification_plan(
                nested, plan(), main_root=self.main, command_runner=self.runner()
            )

    def test_contract_is_non_executing_and_reports_invalid_plan(self):
        contract = verification_execution_contract(plan())
        self.assertEqual(contract["status"], "ready")
        self.assertFalse(contract["execution_performed"])
        self.assertFalse(contract["authority_granted"])
        invalid = verification_execution_contract({"schema": "bad"})
        self.assertEqual(invalid["status"], "invalid_plan")


if __name__ == "__main__":
    unittest.main()
