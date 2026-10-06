# A86 — Anti-Gaming + Regression Firewall v1

Module: `src/sira/anti_gaming.py` (diagnostics: `src/sira/anti_gaming_benchmark.py`).
Called from `evaluate_engineering_candidate` before A85. Advisory evidence only.

## What it does
- Classifies every touched path (implementation, test, benchmark, evaluator, authorization,
  promotion, protected, evaluation artifact, self-model).
- Blocks: deleted tests/test functions, removed assertions, tautological assertions,
  added skip/xfail, lowered thresholds, removed benchmark cases/functions, protected or
  authority path changes, authority self-grant, evaluator bypass markers, test-environment
  detection, hidden-case ids/hashes in candidate code, self-model state literals, secrets.
- Marks suspicious (verdict `inconclusive`): mutable evaluator changes, public-case literal
  hardcoding, unreadable/unparseable changed files, malformed A85 evidence or policy.
- Requires the evaluation suite registry digest to be identical before and after evaluation.
- A candidate with blocking evidence never reaches A85 execution.
- Verdicts: `eligible` | `inconclusive` | `blocked`. Eligible is NOT authorization.
  Evaluator decision codes: `anti_gaming_blocked` (reject), `integrity_inconclusive` (block).

## Honest limits
Static checks over the change-set. No flags is not proof of no gaming. A85 hidden/fresh
evaluation stays a primary defense. If a touched implementation file has no registered
hidden suite, the report records `independent_evaluation.a85_present = false`.

## Owner policy (outside candidate-copyable paths)
`memory/anti_gaming/policy.json`:
`{"schema": "sira.anti_gaming_policy.v1", "require_independent_suite": true,
  "require_target_evidence": true}`
- `require_independent_suite`: touched implementation code without A85 evidence becomes
  `inconclusive` (recommended once suites are registered for important targets).
- `require_target_evidence`: a declared `target_evidence={"kind":..., "improved": bool}` is
  mandatory. A malformed policy file fails closed.

## Evidence
`memory/anti_gaming/reports/ag_<id>.json` + `.sha256` pin (mode 0600). No secrets, no raw
candidate text, bounded size. The self-model may cite eligible reports as scoped,
descriptive evidence; it never changes a capability state by itself.
