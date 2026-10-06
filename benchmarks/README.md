# Benchmark foundation v1

Run `python sira.py benchmark`. The three synthetic replay cases test duplicate
URLs, inert hostile text/invalid links, and empty results. This is a mechanical
regression check, not a research-quality score. Expected URLs are hand-authored.
Cases and expectations are versioned together; their SHA-256 identifies changes.

Each run records a digest of `src/sira/*.py` recursively, provider namespace,
settings, time and run ID. Benchmark reports also record the dataset digest.
Never compare two reports as the same benchmark if their dataset hashes differ.
Do not claim improvement from one latency sample or from unchanged fixture scores.

| Metric | v0.1 meaning |
|---|---|
| factual_accuracy | null: no independently reviewed claims yet |
| citation_correctness | null: semantic support has not been evaluated |
| citation_coverage | null: no generated claim set to evaluate |
| source_quality | null: requires a task-specific review rubric |
| source_diversity | null: hostnames do not establish independent ownership |
| unique_hostnames | Count only; a diversity proxy |
| citation_reference_resolution | Fraction of citation records resolving to their stored source; null if none |
| retrieval_success | At least one valid nonempty source returned, not relevance/accuracy |
| latency_seconds | Retrieval/controller preparation time, excludes final artifact writes |
| api_requests | Attempted network requests this run; cache/fixture = 0; cancellation unknown = null |
| reported_credits | Only service-reported value; unknown after failure = null; cache = 0 |
| failures | Provider failure count (one bounded search); empty result is not an API failure |

To evaluate real improvements later, add a fixed question set, an independently
reviewed expected-fact/source rubric and per-claim support judgments. Use uncached
live runs, record provider settings, repeat latency measurements and keep unseen
holdout tasks outside the mutable agent. Preserve the previous baseline.
Never turn missing scores into zero or 100%. Keep human/final-evaluator scores
separate from the agent's own confidence or provider relevance scores.

The current dataset, tests, Git repository, runs and cache are user-writable.
They are NOT the independent final evaluator or protected snapshots. Before any
self-modification stage, the protected evaluator, benchmark holdout, credential
broker, STOP/PAUSE and permission boundary need separate external enforcement.

## Synthesis structural suite v1

Run `python sira.py benchmark --synthesis`. Five offline cases test supported,
unsupported, mixed, unknown-passage and hostile-text behavior. Expected result is
5 passed, 0 failed, 0 API requests. This suite verifies control flow and the local
gate; fixture verdicts cannot measure model quality, factual accuracy or semantic
citation correctness.

Synthesis records `retrieval_passages`, accepted/rejected claim counts, structural
citation coverage/reference resolution, same-model verifier acceptance, cited
source/hostname counts, cache hits, API calls, token counts and latency. Structural
coverage of 1.0 means every published claim has a locally resolvable citation. It
does not mean the citation actually proves the claim. A future benchmark needs
frozen questions and independent human/final-evaluator labels outside the agent's
write boundary.

## Paper provider structural suite v1

Run `python sira.py benchmark --papers`. Four offline cases cover rich scholarly
metadata, DOI-based deduplication, and a no-result run. Expected result is 3 passed,
0 failed, 0 API requests. The suite hashes its fixture dataset and source code like the
other regression suites. It verifies storage/control behavior only; it does not measure
Semantic Scholar relevance quality, paper quality, or factual correctness.
