# Semantic Scholar Paper Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bounded, anonymous-first Semantic Scholar paper-search command that stores up to three scholarly records with metadata, 24-hour caching, deduplication, audit history, and an offline regression benchmark.

**Architecture:** Keep paper discovery separate from general web `research` so rich scholarly metadata is not squeezed into the existing `Source` contract. `papers.py` owns provider-neutral paper contracts and the run controller; `providers/semantic_scholar.py` performs exactly one bounded GET to the official Academic Graph paper relevance endpoint; `paper_benchmark.py` replays local fixtures with zero network. Existing search/read/answer flows remain unchanged.

**Tech Stack:** Python standard library only (`urllib`, `dataclasses`, JSON); existing `Cache`, `RunStore`, `request_json`, unittest.

**Spec:** User-approved chat design, 2026-09-17: `python sira.py papers "research question"`; Semantic Scholar official API; maximum three papers; title/abstract/authors/year/DOI/URL/open-access PDF metadata; anonymous access first; 24h cache; deduplication; audit/run ID; tests-first offline paper benchmark; no PDF download, Gemini call, or paid service.

## Global Constraints

- Maximum papers per live request: 3.
- Default cache TTL: 86,400 seconds.
- One network request per uncached run; zero automatic retries.
- No PDF download; store only a validated public PDF URL when returned.
- No model/Gemini call in paper search.
- No paid provider dependency or required API key.
- Provider text remains inert data.
- Existing 48 tests must remain green.

---

### Task 1: Provider-neutral paper run contract

**Files:**
- Create: `src/sira/papers.py`
- Test: `tests/test_papers.py`

**Interfaces:**
- Produces: `Paper`, `PaperBatch`, `PaperProvider`, `papers_run(root, query, provider, settings, use_cache=True)`.

- [ ] Write failing tests for bounded query validation, metadata persistence, deduplication, cache hits, and audit events.
- [ ] Run `python -m unittest tests.test_papers.PaperRunTests -v` and verify import/behavior failures.
- [ ] Implement immutable paper contracts, safe JSON serialization/reload, dedupe by paper ID/DOI/URL, 24h cache via existing `Cache`, and `RunStore` audit events.
- [ ] Re-run PaperRunTests and verify green.

### Task 2: Semantic Scholar Academic Graph provider

**Files:**
- Create: `src/sira/providers/semantic_scholar.py`
- Test: `tests/test_papers.py`

**Interfaces:**
- Consumes: `Paper`, `PaperBatch` from `sira.papers`; `request_json` and `open_request`.
- Produces: `SemanticScholarProvider.search(query, max_results)`.

- [ ] Write failing tests asserting the exact official endpoint, encoded GET query, `limit<=3`, exact metadata fields, one request, no key requirement, no redirects/retries, safe response parsing, and open-access URL storage without fetching it.
- [ ] Run provider tests and verify expected failures.
- [ ] Implement a single GET to `https://api.semanticscholar.org/graph/v1/paper/search` requesting only `title,abstract,authors,year,url,externalIds,openAccessPdf`.
- [ ] Parse malformed rows defensively; invalid required metadata is rejected, invalid optional metadata is omitted.
- [ ] Re-run provider tests and verify green.

### Task 3: CLI and offline paper benchmark

**Files:**
- Create: `src/sira/paper_benchmark.py`
- Create: `benchmarks/paper_cases.json`
- Modify: `src/sira/cli.py`
- Test: `tests/test_papers.py`

**Interfaces:**
- Produces CLI `papers <question>` and `benchmark --papers`.

- [ ] Write failing CLI/benchmark tests: paper command emits run/status/count/path and offline paper suite reports all cases passed with zero API requests.
- [ ] Run the tests and verify expected failures.
- [ ] Wire the new command to `SemanticScholarProvider()` and `Settings(root, max_results=3)`; add a mutually-exclusive `--papers` benchmark flag.
- [ ] Implement local fixture replay in `paper_benchmark.py` and dataset hashing.
- [ ] Re-run CLI/benchmark tests and verify green.

### Task 4: Version/docs and regression verification

**Files:**
- Modify: `src/sira/__init__.py`
- Modify: `README.md`
- Modify: `docs/PROVIDERS.md`
- Modify: `benchmarks/README.md`
- Create: `docs/PAPERS.md`

- [ ] Bump SIRA to `0.4.0` and document anonymous Semantic Scholar behavior, current official endpoint/fields/rate-limit caveats, cache, output artifacts, and explicit no-PDF-download guarantee.
- [ ] Run `python -m unittest discover -s tests -v` and require zero failures.
- [ ] Run `python sira.py benchmark`, `python sira.py benchmark --reading`, `python sira.py benchmark --synthesis`, and `python sira.py benchmark --papers`; require zero failures and zero API requests from all benchmark suites.
- [ ] Run a mocked CLI smoke test and inspect output/audit artifacts.
