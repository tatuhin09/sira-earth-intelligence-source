# SIRA v0.4.9 — Gemini Engineering Timeout Headroom

## Evidence
A real v0.4.8 engineering handoff no longer failed with `output_truncated`.
Instead, the engineering provider exhausted all three bounded attempts with
`network_or_timeout`.

The engineering provider was still using a 30-second default request timeout,
while v0.4.8 increased its bounded output budget to 65536 tokens.

## Change
Increase only the default `GeminiEngineeringModel` request timeout from
30 seconds to 60 seconds.

## Deliberately unchanged
- model id
- 65536 output-token budget
- response JSON schema
- retryable error set
- maximum attempts (3)
- retry/backoff policy
- v0.4.7 sanitized response diagnostics
- writer context contract
- evaluator/promotion authority
- package-install authority
- paid-spending authority
- protected runtime boundary

The existing constructor validation remains `1..60`, so 60 seconds is already
inside the previously accepted bounded contract.
