# SIRA v1.8E — Multi-Language Writer + Isolated Verification Integration

v1.8E adds a parallel engineering writer path without widening SIRA's existing
Python self-modification/promotion path.

Flow:

1. detect the source project's supported language policy;
2. build bounded source context with secret/vendor/build exclusions;
3. ask the engineering model for structured complete-file source edits;
4. allow at most one exact-path context expansion;
5. route edits through the v1.8D engineering candidate boundary;
6. merge/deduplicate the per-file v1.8A verification plans;
7. execute the merged plan through the v1.8B isolated verifier;
8. expose v1.8C structured diagnostics as advisory evidence;
9. mark the candidate verified, blocked, rejected, or no-edit;
10. never promote or modify the main tree.

A separate Gemini engineering provider uses the same bounded retry discipline as
the existing code provider, but its system instruction is project-generic. It
forbids dependency/lock/build-config edits, package installation, privilege
requests, and verification claims.

Existing source targets must be supplied in full model context before they may
be edited. The model gets at most one bounded expansion pass and four exact
context paths.

Verification remains fail-closed. v1.8D intentionally excludes dependency
trees such as `node_modules`, `.venv`, Gradle/Cargo build outputs, and similar
artifacts from candidate copies. Therefore projects that require unavailable
local dependencies may correctly return verification blocked/failed. v1.8E
does not weaken isolation to manufacture a pass.

This stage does not connect the engineering writer to the autonomous runtime,
Evaluator1, Evaluator2, Promotion, or autonomous promotion. Those existing
SIRA protected flows remain unchanged.
