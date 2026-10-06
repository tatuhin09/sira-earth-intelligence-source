# SIRA v0.4.4 — Gemini Code Response Resilience

Production evidence showed repeated code-worker failures dominated by
`invalid_response`, plus `http_503` and `network_or_timeout`.

The code writer requires complete UTF-8 file contents for edits, while the
Gemini code client capped output at 8192 tokens. Gemini 3.1 Flash-Lite supports
65536 output tokens, so this release aligns the writer cap with that model
limit.

The parser now distinguishes `MAX_TOKENS` as `output_truncated`, distinguishes
safety/block finishes as `response_blocked`, and preserves the actual request
attempt count for malformed responses. Raw provider response text is never
persisted.

Candidate validation, evaluators, authorization, promotion, rollback, protected
shell, payment, and package-installation authority are unchanged.
