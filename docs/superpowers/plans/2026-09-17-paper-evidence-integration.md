# Paper Evidence Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert scholarly paper runs into provenance-checked evidence runs that the existing verified synthesis engine can consume, preferring abstracts and using bounded open-access PDF extraction only when an abstract is unavailable.

**Architecture:** Add a paper-parent loader and paper evidence runner that emit the existing `source_evidence` contract with a `papers.json` parent artifact and paper-specific provenance. Add a dependency-free conservative PDF text reader for fallback only; failures remain per-paper and never poison successful abstracts. Generalize evidence loading to validate either `result.json` or `papers.json` parents while preserving backward compatibility.

**Tech Stack:** Python 3 standard library, unittest, existing SIRA RunStore/Cache/evidence/synthesis components.

**Spec:** Approved in chat on 2026-09-17: abstract-first, free/open-access-only PDF fallback, exact provenance, existing verifier reuse, failure isolation, prompt-injection inertness, cache/audit/offline benchmark, no paid API requirement.

## Global Constraints

- No mandatory paid API or new third-party dependency.
- At most three papers per paper run.
- Abstract text is preferred and requires no network call.
- PDF fallback is bounded by URL safety, response size, extracted-text size, timeout, and conservative parsing.
- One failed PDF must not fail other paper evidence.
- Paper text is untrusted data and must never be executed as instructions.
- Existing web evidence and synthesis behavior must remain compatible.
- Every persisted paper-evidence run must bind cryptographically to its `papers.json` parent and stored text.

---

### Task 1: Paper parent integrity and abstract-first evidence

**Files:**
- Create: `src/sira/paper_reading.py`
- Create: `tests/test_paper_reading.py`
- Modify: `src/sira/retrieval.py`

**Interfaces:**
- Produces `load_paper_parent(root, run_id) -> (dict, sha256)`.
- Produces `paper_read_run(root, paper_run_id, pdf_reader, use_cache=True) -> Path`.
- Emits schema-version-1 `source_evidence` with `parent_artifact="papers.json"` and P1..P3 document IDs.

- [ ] Write tests for parent traversal/tampering and abstract-first no-network behavior.
- [ ] Run tests and confirm feature-missing failures.
- [ ] Implement minimal parent loader and abstract evidence writer.
- [ ] Run tests green.

### Task 2: Bounded open-access PDF text fallback

**Files:**
- Create: `src/sira/providers/pdf_text.py`
- Modify: `src/sira/paper_reading.py`
- Modify: `tests/test_paper_reading.py`

**Interfaces:**
- Produces `PdfTextReader.read(urls) -> ExtractBatch` compatible with existing document contracts.
- Accepts only public HTTP(S) URLs and PDF payloads; rejects encrypted/oversized/unextractable inputs safely.

- [ ] Write failing tests for simple PDF text, invalid/private/oversized/encrypted payloads, and per-paper failure isolation.
- [ ] Run tests red.
- [ ] Implement minimal bounded downloader/parser and fallback integration.
- [ ] Run tests green.

### Task 3: Existing synthesis compatibility and CLI

**Files:**
- Modify: `src/sira/retrieval.py`
- Modify: `src/sira/cli.py`
- Modify: `src/sira/__init__.py`
- Modify: `tests/test_paper_reading.py`

**Interfaces:**
- Existing `answer <evidence_run_id>` accepts paper evidence without a parallel synthesis engine.
- New CLI: `python sira.py paper-read <paper_run_id> [--no-cache]`.

- [ ] Write failing end-to-end test that paper evidence is accepted by `load_evidence_run` / packet builder and CLI outputs status/metrics.
- [ ] Run tests red.
- [ ] Generalize parent-artifact validation and add CLI wiring/version bump.
- [ ] Run tests green plus existing synthesis tests.

### Task 4: Offline regression benchmark and docs

**Files:**
- Create: `src/sira/paper_reading_benchmark.py`
- Create: `benchmarks/paper_reading_cases.json`
- Modify: `src/sira/cli.py`
- Modify: `README.md`
- Modify: `docs/PAPERS.md`
- Modify: `tests/test_paper_reading.py`

**Interfaces:**
- New benchmark selector: `python sira.py benchmark --paper-reading`.
- Fixture benchmark performs zero network/API requests.

- [ ] Write failing benchmark CLI test.
- [ ] Run red.
- [ ] Implement fixture benchmark and docs.
- [ ] Run green.
- [ ] Run compileall, full unittest discovery, and all five benchmark suites.
