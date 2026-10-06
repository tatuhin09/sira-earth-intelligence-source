# Gemini Engineering Output Budget v0.4.8

## Evidence

The autonomous engineering route uses `GeminiEngineeringModel` with the same
`gemini-3.1-flash-lite` model as the generic code writer. Generation 16 produced
a sanitized `ProviderError(code="output_truncated", request_count=1)` and the
engineering request was configured with `maxOutputTokens: 8192`, while the
already-validated generic code route uses a 65536-token output budget.

## Change

* Add a local `MAX_OUTPUT_TOKENS = 65536` constant to the engineering provider.
* Bind `generationConfig.maxOutputTokens` to that constant.
* Do not change retries, parsing, schemas, writer context, evaluator gates,
  promotion authority, package-install authority, or spending authority.
* Add an offline regression proving the request uses the larger bounded budget
  and that `MAX_TOKENS` remains sanitized/non-retryable with request_count=1.

## Validation intent

The installer is bound to baseline `32c0f43`, requires a clean tree and the
runtime already OFF/stopped, validates payload hashes, performs no live provider
call, runs targeted provider/writer/runtime-resilience tests, then the complete
offline unittest regression before committing.
