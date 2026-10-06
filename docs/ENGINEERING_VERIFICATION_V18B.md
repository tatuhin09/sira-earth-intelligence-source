# SIRA v1.8B — Isolated Engineering Verification Adapters

v1.8B turns v1.8A verification plans into an executable but fail-closed
candidate-only verifier.

Production execution requires Bubblewrap. The verifier uses a private network
namespace, read-only host filesystem, one writable candidate bind, no shell,
no inherited credentials, no package installation, bounded command count,
bounded timeout, bounded output, and aggregate-only result reporting.

If Bubblewrap or a required tool is unavailable, verification blocks before the
project command runs. There is deliberately no unsandboxed fallback.

Supported adapter families remain those emitted by v1.8A: Python unittest /
pytest; npm/pnpm/yarn/bun project scripts; TypeScript no-emit typecheck;
Dart/Flutter analyze and test; Maven/Gradle offline test; CMake/CTest with
FetchContent disconnected; Cargo offline check/test; Go test with
GOPROXY/GOSUMDB disabled; and SQLFluff lint.

The execution result is non-authoritative. It cannot authorize promotion and
does not change Evaluator1, Evaluator2, Promotion, autonomous promotion,
runtime, CLI, or the self-modification boundary. Existing SIRA Python
promotion verification continues unchanged.
