# SIRA v1.8C — Structured Engineering Diagnostics

Verifier stdout/stderr is parsed only in memory into bounded structured diagnostics.
Raw verifier output is not persisted. Supported formats cover Python, TypeScript/ESLint,
Dart/Flutter, Java, C/C++, Rust, Go, and SQLFluff.

Each diagnostic contains tool/command, language, severity, optional code, safe project-relative
path, line/column, redacted bounded message, and a stable SHA-256 fingerprint. Fingerprints
exclude line/column so the same failure can remain recognizable after line movement.

Absolute paths outside the project/candidate root are discarded and credential-like values are
redacted. Diagnostics are advisory repair evidence only and cannot authorize writing or promotion.
