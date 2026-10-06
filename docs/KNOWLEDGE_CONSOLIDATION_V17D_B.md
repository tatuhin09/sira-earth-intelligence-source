# SIRA v1.7D-B — Verified Knowledge Consolidation

This stage adds a separate local knowledge database at `memory/sira_knowledge.sqlite3`.

Automatic durable learning is deliberately stricter than search/retrieval. Search-result metadata does not become factual knowledge. Only claims accepted by the existing synthesis acceptance gate may enter the automatic bridge. The current verifier is explicitly recorded as a same-model second pass, not an independent model.

Reusable knowledge requires at least two distinct source URLs, at least two distinct hostnames, confidence at or above the local threshold, and no conflicting verified value under the same knowledge key. A single source remains pending. Duplicate evidence is idempotent, and a synthesis cache replay does not create new learning evidence.

Conflicting verified values fail closed. Previously active knowledge becomes `conflicted` and is excluded from default local reuse. This stage does not guess semantic identity across differently worded contradictions; explicit knowledge keys are the conflict boundary.

Each consolidated claim stores `last_verified_at` and a bounded staleness horizon. Stale claims remain auditable, are excluded from default search, and are surfaced through `revalidation_candidates()`.

Knowledge grants no owner, billing, spending, evaluator, or promotion authority. It is advisory evidence only.

Python 3.14 SQLite resource hygiene is also tightened for the remaining direct memory benchmark/test connections by using `contextlib.closing`.
