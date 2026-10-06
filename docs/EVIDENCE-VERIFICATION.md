# v0.2 verification — 2026-09-17

- Python 3.12: all 34 unit/integration tests passed.
- Existing retrieval benchmark: 3/3 passed, zero API requests.
- New reading benchmark: 4/4 passed, zero API requests.
- Reading code SHA-256: `9e26bc8a3882faf932c5d8515d6f10afab19c2f4374ceaef3da76b1ea3e8d71c`.
- Independent code review found an invalid-Unicode failure path; reproduced,
  fixed and covered by a regression test before delivery.
- Runtime dependencies: Python standard library only.
- Live user-authenticated extraction: pending user-side run.
- Factual accuracy and independent claim verification: not evaluated.
- The user's previous search/cache passed live on Python 3.14; this report does
  not claim v0.2 has already been executed on the user's PC.

Commands: `python -m unittest discover -s tests -v`, `python sira.py benchmark`,
`python sira.py benchmark --reading`.

`benchmarks/baseline-reading-v1.json` preserves the synthetic baseline. Its run
IDs refer to the original build-time runs. New invocations create fresh runs.
