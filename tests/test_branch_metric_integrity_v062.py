"""A conditional expression must carry the same branch cost as an if statement."""
import ast
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.autonomous_promotion import _branch_points_for_symbol, _structural_goal_check
from sira.opportunity_detectors import branch_points


BASELINE = """def target(flag):
    if flag:
        result = 1
    else:
        result = 0
    return result
"""

CANDIDATE = """def target(flag):
    result = 1 if flag else 0
    return result
"""


class BranchMetricIntegrityV062Tests(unittest.TestCase):
    def test_scanner_and_protected_gate_count_equivalent_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            candidate = Path(tmp) / "candidate"
            for root, content in ((main, BASELINE), (candidate, CANDIDATE)):
                target = root / "src/sira/demo.py"
                target.parent.mkdir(parents=True)
                target.write_text(content, encoding="utf-8")
            baseline_node = ast.parse(BASELINE).body[0]
            candidate_node = ast.parse(CANDIDATE).body[0]
            self.assertEqual(branch_points(baseline_node), branch_points(candidate_node))
            old = _branch_points_for_symbol(main, "src/sira/demo.py", "target")
            new = _branch_points_for_symbol(candidate, "src/sira/demo.py", "target")
            self.assertEqual(old, new)
            self.assertGreaterEqual(old, 1)
            goal = _structural_goal_check(main, candidate, {
                "target": {"path": "src/sira/demo.py", "symbol": "target"},
                "success_criteria": {"structural_goal": {
                    "metric": "branch_points", "baseline": old, "target_max": old - 1,
                }},
            })
            self.assertEqual(goal["decision"], "fail")
            self.assertEqual(goal["decision_code"], "structural_goal_not_met")


if __name__ == "__main__":
    unittest.main()
