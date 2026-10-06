from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/"src"))
from sira.engineering_diagnostics import parse_diagnostics,diagnostic_advisory_context

class EngineeringDiagnosticsTests(unittest.TestCase):
    def test_multi_language(self):
        fixtures=[
            ("typescript.typecheck","typescript","src/a.ts(12,5): error TS2322: bad type"),
            ("node.lint","typescript","src/a.ts\n  9:3  warning  Unexpected any  rule-x"),
            ("flutter.analyze","dart","error • Undefined name x • lib/main.dart:5:7 • undefined_identifier"),
            ("java.maven_test","java","src/main/java/A.java:[7,11] cannot find symbol"),
            ("cmake.build","cpp","src/a.cpp:4:9: error: undeclared x"),
            ("rust.check","rust","error[E0308]: mismatched types\n --> src/lib.rs:8:5"),
            ("go.test","go","./main.go:3:2: undefined: x"),
            ("sql.sqlfluff","sql","L: 2 | P: 4 | LT01 | spacing"),
        ]
        for cid,lang,text in fixtures:
            with self.subTest(cid=cid):
                r=parse_diagnostics(cid,lang,text)
                self.assertGreaterEqual(r["diagnostic_count"],1)
                self.assertEqual(len(r["diagnostics"][0]["fingerprint"]),64)
                self.assertFalse(r["raw_output_included"])

    def test_python_absolute_path_and_secret_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); p=root/"tests"/"test_x.py"; p.parent.mkdir(); p.write_text("x\n")
            text=f'Traceback\n  File "{p}", line 12, in test_x\nAssertionError: token=secret123 failed'
            r=parse_diagnostics("python.unittest","python",text,root=root)
            self.assertEqual(r["diagnostics"][0]["path"],"tests/test_x.py")
            self.assertNotIn("secret123",repr(r))

    def test_external_absolute_path_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=parse_diagnostics("go.test","go","/home/private/main.go:4:2: undefined: x",root=Path(tmp))
            self.assertIsNone(r["diagnostics"][0]["path"])
            self.assertNotIn("/home/private",repr(r))

    def test_fingerprint_stable_across_line_shift(self):
        a=parse_diagnostics("go.test","go","./main.go:3:2: undefined: x")
        b=parse_diagnostics("go.test","go","./main.go:30:2: undefined: x")
        self.assertEqual(a["diagnostics"][0]["fingerprint"],b["diagnostics"][0]["fingerprint"])

    def test_advisory_non_authoritative(self):
        p=parse_diagnostics("go.test","go","./main.go:3:2: undefined: x")
        a=diagnostic_advisory_context({"commands":[{"diagnostics":p["diagnostics"]}]})
        self.assertEqual(a["repair_authority"],"advisory_only")
        self.assertFalse(a["promotion_authorized"])
