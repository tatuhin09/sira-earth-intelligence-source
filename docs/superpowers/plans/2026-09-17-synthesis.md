# Evidence Verification and Answer Synthesis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a source-grounded answer from an existing evidence run and reject unsupported model claims.

**Architecture:** Validate the evidence chain, retrieve bounded passages, run structured proposal and verification model calls, enforce a local acceptance gate, and render only accepted claims.

**Tech Stack:** Python 3.10+ standard library, Gemini REST API, unittest, Git.

**Spec:** `docs/superpowers/specs/2026-09-17-synthesis-design.md`

## Global Constraints

- Preserve v0.1 search and v0.2 reading formats and commands.
- No local model, browser, vector database or non-standard Python dependency.
- Treat source and model text as untrusted data; never execute it.
- Factual accuracy and objective citation correctness remain null.
- Two API calls maximum on uncached success; zero when both cached.

---

### Task 1: Evidence-chain loader and lexical retriever

**Files:** Create `src/sira/retrieval.py`; create `tests/test_synthesis.py`.

**Interfaces:** `load_evidence_run(root, run_id)` returns validated documents and parent hash. `LexicalRetriever.retrieve(question, documents)` returns bounded `Passage` objects with literal offsets.

- [ ] Write tests that fail because retrieval module does not exist.
- [ ] Verify failure with `python -m unittest tests/test_synthesis.py -v`.
- [ ] Implement hash/path/schema validation, bounded chunking and deterministic ranking.
- [ ] Verify exact offsets, relevance, deterministic order and tamper rejection pass.

### Task 2: Provider-neutral synthesis and local gate

**Files:** Create `src/sira/synthesis.py`; create `src/sira/providers/gemini.py`; modify `src/sira/config.py`; modify `tests/test_synthesis.py`.

**Interfaces:** `SynthesisModel.propose(question, passages)` and `.verify(question, claims, passages)` return structured batches. `synthesize_run(...)` writes an immutable answer run.

- [ ] Add failing tests for supported, mixed, unknown-reference, malformed, network-failure and cache paths.
- [ ] Implement strict proposal/verdict validators and deterministic accepted-claim rendering input.
- [ ] Implement bounded Gemini structured-output transport with key header, two calls, no retry/tools and usage accounting.
- [ ] Verify unsupported claims never appear in final answer and secrets never enter artifacts.

### Task 3: Reports, CLI and offline benchmark

**Files:** Create `src/sira/answer_reports.py`; create `src/sira/synthesis_benchmark.py`; create `benchmarks/synthesis_cases.json`; modify `src/sira/cli.py`; modify `src/sira/__init__.py`; modify docs and tests.

**Interfaces:** `answer EVIDENCE_RUN_ID`; `benchmark --synthesis`; `setup-model-key`.

- [ ] Add failing CLI/benchmark/rendering tests.
- [ ] Implement escaped HTML, Markdown citations, structured JSON summary and offline fixture model.
- [ ] Run all tests and all three benchmark suites.
- [ ] Review error/security paths, fix demonstrated issues with regression tests.

### Task 4: Guarded user update

**Files:** Create update package containing full changed/new files, manifest, updater and README.

**Interfaces:** `python3 apply_update.py --project "$HOME/sira"`.

- [ ] Generate pre/post SHA-256 manifest from the v0.2 baseline.
- [ ] Verify clean install preserves `.env`, prior runs/cache and Git history.
- [ ] Verify reapply, modified-target refusal, symlink refusal and simulated rollback.
- [ ] Extract package fresh, run tests and three offline benchmarks, then provide exact Ubuntu commands.
