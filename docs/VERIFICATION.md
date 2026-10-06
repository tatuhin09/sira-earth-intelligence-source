# Starter verification — 2026-09-17

- Python: 3.12.14 (Linux execution environment).
- Runtime dependencies: standard library only.
- Source package SHA-256: `11c38cf00132bff79e6222ea99424ee2ea6ea73c9ae99c2899f9b87fa350d65c`.
- Unit/integration tests: 18 passed, zero failures.
- Offline regression: 3 passed, zero failures, zero API requests.
- Offline research and repeated-query cache hit: verified.
- `.env`, `.env.local`, `.venv`, `.cache`, and generated runs ignored by Git: verified.
- Review found truncated-response and extreme-credit error paths; both reproduced,
  fixed, and covered by regression tests.
- Live authenticated Tavily request: NOT RUN (user key required).
- Factual accuracy and semantic citation evaluation: NOT IMPLEMENTED in this slice.
- Self-modification, terminal autonomy, external evaluator/STOP/broker: NOT IMPLEMENTED.

Reproduce from the project root:

```bash
python -m unittest discover -s tests -v
python sira.py benchmark
python sira.py research "offline duplicate example" --provider fixture
```

`benchmarks/baseline-fixture-v1.json` preserves this synthetic baseline. Its run IDs
identify the original build-time runs; those scratch run directories are not
included in the starter package. Your invocation creates new run IDs and files.
This report does not certify research quality or future improvement.

## v0.4 paper-provider verification — 2026-09-17

- Semantic Scholar unit/controller/CLI tests added: 11.
- Full suite after integration: 59 passed, zero failures.
- `python -m compileall -q src tests`: passed.
- Retrieval benchmark: 3/3 passed, 0 API requests.
- Reading benchmark: 4/4 passed, 0 API requests.
- Synthesis benchmark: 5/5 passed, 0 API requests.
- Paper benchmark: 3/3 passed, 0 API requests.
- Provider contract tests verify one anonymous GET, maximum three results, explicit
  metadata fields, no PDF fetch, no automatic retry, safe malformed-row handling,
  24-hour controller cache and DOI/URL/paper-ID deduplication.
- A live Semantic Scholar request was not completed in the build sandbox because
  outbound DNS resolution was unavailable. Official endpoint/field/rate-limit
  contracts were checked against Semantic Scholar documentation on 2026-09-17.

## v0.4.1 Semantic Scholar rate-limit resilience

This update adds bounded 429 retry, actual request-attempt accounting, and an optional
Semantic Scholar API key loaded literally from environment/`.env`. Network behavior is
covered with mocked HTTP responses; the paper benchmark remains offline. A live success
is not claimed by package tests because external throttling is outside SIRA's control.


## v0.5 free multi-provider paper failover

Verification adds mocked Crossref transport/parsing tests and provider-router tests. The
default paper CLI is tested to fall back from a synthetic Semantic Scholar 429 to a
Crossref result without user intervention. The failure path also verifies aggregate
request counts and provider-attempt diagnostics while keeping raw HTTP bodies out of
artifacts. All paper-provider tests remain offline.

## v0.6 arXiv enrichment verification — 2026-09-17

- Added mocked arXiv Atom transport/parsing tests, hostile XML rejection, HTTP error accounting,
  cross-provider DOI/title enrichment, provider provenance, recovery, and forced-provider CLI tests.
- Default auto mode is regression-tested so Semantic Scholar/Crossref behavior remains bounded and
  arXiv enrichment is explicitly mocked; unit tests perform no live scholarly API calls.
- Paper benchmark expanded to 4 offline cases, including arXiv-style date/provenance metadata.
- Fresh full suite: 85 tests passed, zero failures; `python -m compileall -q src tests` passed.
- Offline benchmarks: retrieval 3/3, reading 4/4, synthesis 5/5, papers 4/4; every benchmark reported `api_requests: 0`.
- Live arXiv success is not claimed by offline package tests; external availability is verified only by the user live run.
