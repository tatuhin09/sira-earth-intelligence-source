# Memory v1.3 Batch 3 — Retrieval Feedback

Schema v4 adds append-only memory outcome feedback and an explainable retrieval
API while preserving legacy `search()` behavior.

## Outcome events

Supported outcome types:

- `retrieval_used`
- `retrieval_helped`
- `retrieval_irrelevant`
- `application_succeeded`
- `application_failed`

Events are append-only, bounded, auditable, and do not change memory lifecycle
status.

## Retrieval

`MemoryStore.retrieve(query, limit, autonomous=False)` combines:

- lexical relevance
- deterministic memory confidence
- bounded freshness/decay
- recurrence
- source diversity
- reuse feedback
- contradiction penalty

Every result contains a bounded `retrieval_score` and `score_reasons`.

Old memories are not deleted merely because they are stale. Synthetic-only
memories remain inspectable but are excluded when `autonomous=True`.

Legacy `search()` is intentionally unchanged in Batch 3.
