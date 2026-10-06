# SIRA v1.1 Multi-Signal Opportunity Intelligence Design

## Scope

Extend proactive opportunity discovery from complexity-heavy scanning to a deterministic local detector registry. The first v1.1 batch adds four function-level signals: complex functions, missing direct test references, silently swallowed broad exceptions, and exact duplicate function bodies within one module.

## Detector contract

- Discovery remains local-only: zero network/model calls and no source mutation.
- Protected-shell files remain excluded before detector execution.
- Existing complexity thresholds and fingerprints remain stable so prior cooldown history is not invalidated merely by the v1.1 scanner upgrade.
- Missing-test detection is conservative: only public top-level functions with meaningful size/control-flow and no matching test identifier are flagged.
- Weak-error detection flags only clearly swallowed bare/`Exception`/`BaseException` handlers whose body is passive (`pass`, `continue`, or `break`); return-based fallbacks are not classified as weak automatically.
- Duplication detection requires exact normalized AST body equality among top-level functions in the same module, ignoring only a leading docstring.

## Evidence and research contract

Every signal must pass local evidence enrichment before external research. Existing-code refactors require mapped benchmarks and existing test references. Missing-test opportunities cannot require the missing test as evidence, so they instead require a mapped benchmark plus a real local caller. Research queries, relevance terms, and candidate strategies are specific to the signal type.

## Measurable goals

- `complex_function`: reduce `branch_points` to the evidence-derived maximum.
- `missing_test_reference`: add at least one direct test call to the exact target module/function.
- `weak_error_handling`: reduce `weak_exception_handlers` by at least one.
- `duplicate_function_body`: reduce exact same-module `duplicate_body_matches` by at least one.

The trusted autonomous-promotion gate independently recomputes these metrics in the main and candidate trees. It does not import the modifiable detector implementation, preserving the protected trust boundary.

## Compatibility and safety

Existing benchmark, Evaluator 2, Evaluator 1, transactional promotion/rollback, exact-fingerprint cooldown, lineage cooldown, smart model-context budget, and runtime reliability rules remain unchanged. New opportunities cannot authorize themselves based only on detector output.
