# SIRA v0.5.0 — Python symbol-scoped engineering edits

The real `src/sira/memory.py:retrieve` target is about 7.6 KB, while the
existing writer contract requires a complete replacement of the entire
~75.5 KB `memory.py`. With 65536 output tokens and a 60-second request timeout,
the known real handoff still exhausted all three attempts with
`network_or_timeout`.

v0.5.0 adds a fail-closed Python symbol-scoped output mode. For one
unambiguous Python target symbol, Gemini returns the replacement target
definition plus optional private sibling helpers instead of the whole file.
SIRA locally reconstructs the complete file after verifying the captured
source SHA-256, path, symbol, unchanged signature/decorators, syntax, helper
bounds and byte limits.

The existing complete-file candidate editor, verification, Evaluator 2,
protected Evaluator 1, transactional promotion and rollback chain remain
unchanged downstream. Legacy full-file edits remain available when no safe
Python symbol scope exists. Non-Python behavior is unchanged.

No retry, timeout, token-budget, package-install, paid-spending, evaluator,
promotion, runtime scheduling, protected-path or rollback authority is added.
