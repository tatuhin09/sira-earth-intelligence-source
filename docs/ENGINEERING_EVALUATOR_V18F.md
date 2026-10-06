# SIRA v1.8F — Independent Engineering Evaluator Gate

v1.8F adds a parallel, non-authoritative evaluator for the multi-language
engineering path. It does not modify or replace SIRA's protected Evaluator1,
Evaluator2, Promotion, autonomous promotion, runtime, or existing Python
self-modification path.

The engineering evaluator independently re-checks:

- the v1.8E writer-attempt schema and verified-ready status;
- writer/edit/candidate manifest non-authority contracts;
- exact source-root and candidate-root binding;
- the original source-copy SHA against the current main engineering surface;
- declared changed paths against the v1.8D language/path policy;
- the actual main-to-candidate diff against the exact declared changed set;
- candidate file hashes against the v1.8D edit report;
- no source removals;
- no symlinks on the non-generated candidate surface;
- no sensitive artifacts such as `.env` or key material;
- no unexpected non-source artifacts on the non-generated surface;
- UTF-8 text and existing edit size/count budgets;
- v1.8B verification actually executed, completed, and passed;
- every verification command passed without timeout/output-limit failure;
- no missing verification commands;
- zero structured diagnostics;
- a clean v1.8C advisory with no promotion authority.

A passing report returns:

`recommendation = eligible_for_protected_gate`

but always keeps:

- `promotion_authorized = false`
- `promotion_performed = false`
- `authority_granted = false`
- `main_tree_modified = false`

A changed main project after candidate creation blocks the candidate as stale.
Structural tampering or non-clean verification rejects the candidate.

This stage still does not perform engineering promotion and is not connected to
the autonomous runtime. A future stage may consume this recommendation only
through a separately protected authorization and transactional promotion design.
