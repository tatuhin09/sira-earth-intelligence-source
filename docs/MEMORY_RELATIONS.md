# Memory v1.3 Batch 2 — Relations, Clustering, Contradictions

Schema version 3 adds conservative memory relationship metadata without changing exact-fingerprint deduplication or automatic lifecycle authority.

## Invariants

- Exact fingerprint deduplication still happens before clustering.
- Near-duplicate memories remain separate memory rows.
- Clustering is contextual and deterministic.
- `similar` and `contradicts` relations are symmetric and idempotent.
- Contradictions never auto-delete, auto-reject, or auto-supersede memories.
- Synthetic-only memories cannot add contradiction penalties to real memories.
- Relation evidence remains auditable through `MemoryStore.show()`.
- Reconciliation proposes relations; it does not silently persist them.

## Schema v3

`memories` gains `cluster_key` and `cluster_size`. A new `memory_relations` table stores canonical symmetric memory pairs, relation type, confidence, reason, bounded JSON evidence, and creation time. The v1 → v2 → v3 migration is additive and preserves existing history.
