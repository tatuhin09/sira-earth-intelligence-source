# SIRA v1.8A — Project & Language Intelligence Foundation

v1.8A adds deterministic, local-only engineering inspection for Python,
JavaScript/TypeScript, Dart/Flutter, Java, C/C++, Rust, Go, and SQL.

The profile includes manifests, frameworks, build systems, package managers,
test runners, and locally visible tool availability. Detection performs no
subprocess execution, package installation, network request, login, payment,
or source mutation.

`plan_verification()` emits structured argv arrays, never shell strings.
Plans are candidate-only and require network-disabled execution. Ecosystems
with explicit offline modes use them directly (`mvn -o`, Gradle/Cargo
`--offline`, `GOPROXY=off`, disconnected CMake FetchContent).

v1.8A does not expand SIRA's edit boundary. Opportunity discovery and
self-modification remain Python/SIRA-scoped. Opportunity evidence now carries
the read-only engineering profile and verification plan so later v1.8 adapters
can consume one stable contract.

No engineering profile or verification plan can authorize promotion, modify
protected policy, install dependencies, or grant owner/spending authority.
