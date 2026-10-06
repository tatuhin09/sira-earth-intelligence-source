from pathlib import Path
import tempfile
from .engineering_diagnostics import parse_diagnostics, diagnostic_advisory_context
from .storage import RunStore, write_json

def engineering_diagnostics_benchmark(root: Path):
    cases=[]
    add=lambda i,p: cases.append({"case_id":i,"passed":bool(p)})
    with tempfile.TemporaryDirectory() as tmp:
        project=Path(tmp)
        fixtures=[
            ("typescript.typecheck","typescript","src/a.ts(12,5): error TS2322: bad type"),
            ("node.lint","typescript","src/a.ts\n  9:3  warning  Unexpected any  rule-x"),
            ("flutter.analyze","dart","error • Undefined name x • lib/main.dart:5:7 • undefined_identifier"),
            ("java.maven_test","java","src/main/java/A.java:[7,11] cannot find symbol"),
            ("cmake.build","cpp","src/a.cpp:4:9: error: undeclared x"),
            ("rust.check","rust","error[E0308]: mismatched types\n --> src/lib.rs:8:5"),
            ("go.test","go","./main.go:3:2: undefined: x"),
            ("sql.sqlfluff","sql","L: 2 | P: 4 | LT01 | spacing"),
            ("python.unittest","python",'Traceback\n  File "/tmp/tests/test_x.py", line 12, in test_x\nAssertionError: nope'),
        ]
        for cid,lang,text in fixtures:
            r=parse_diagnostics(cid,lang,text,root=project)
            add(cid,r["diagnostic_count"]>=1 and not r["raw_output_included"])
        a=parse_diagnostics("go.test","go","./main.go:3:2: undefined: x")
        b=parse_diagnostics("go.test","go","./main.go:30:2: undefined: x")
        add("stable_fingerprint",a["diagnostics"][0]["fingerprint"]==b["diagnostics"][0]["fingerprint"])
        s=parse_diagnostics("go.test","go","./main.go:3:2: token=abc123 failed")
        add("secret_redacted","abc123" not in repr(s))
        adv=diagnostic_advisory_context({"commands":[{"diagnostics":a["diagnostics"]}]})
        add("advisory_only",adv["repair_authority"]=="advisory_only" and not adv["promotion_authorized"])
    report={"schema_version":1,"kind":"engineering_diagnostics_benchmark","suite_id":"sira-engineering-diagnostics-v1.8c",
            "passed":sum(x["passed"] for x in cases),"failed":sum(not x["passed"] for x in cases),"api_requests":0,"cases":cases}
    store=RunStore(Path(root).resolve()); path=store.path/"engineering-diagnostics-benchmark.json"; write_json(path,report)
    return path,report
