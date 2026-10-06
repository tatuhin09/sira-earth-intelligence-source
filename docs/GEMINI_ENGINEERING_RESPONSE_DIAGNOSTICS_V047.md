# Gemini Engineering Response Diagnostics — v0.4.7

## Evidence and scope

The autonomous engineering route constructs `GeminiEngineeringModel`, which has its own structured-response parser. The prior Gemini code-provider diagnostic patch therefore did not classify failures on this engineering path.

This patch changes only response decoding in `GeminiEngineeringModel.generate_patch`. The request contract, model selection, retryable-code set, retry/backoff loop, engineering writer, candidate validation, evaluators, promotion, runtime controls, payment authority, package installation authority, and protected boundary are unchanged.

## Sanitized response failure codes

The engineering parser now emits bounded ProviderError codes rather than a generic parser bucket:

- `missing_candidates`
- `invalid_candidate_shape`
- `missing_content`
- `missing_parts`
- `empty_response_text`
- `malformed_json`
- `json_not_object`
- `output_truncated` for `MAX_TOKENS`
- `response_blocked` for prompt blocking / bounded safety categories
- `response_recitation`
- `unsupported_language`
- `generation_stopped_other`
- `malformed_function_call`
- `generation_stopped_unknown` for every other non-success finish reason
- `invalid_usage_metadata` when token metadata has an invalid container shape

No raw response, generated text, prompt, API key, finish message, or raw unknown finish-reason value is put into those codes.

All post-request parser failures bind `request_count` to the actual provider attempt number. Existing retry behavior is not expanded.

## Validation

Installer gates:

1. exact HEAD `6165023`;
2. clean working tree;
3. runtime OFF/stopped;
4. payload SHA-256 verification;
5. AST/source-contract match for the unique engineering response parser;
6. static compilation;
7. targeted provider/writer/resilience tests;
8. full test discovery;
9. exact three-file changeset;
10. commit only after all tests pass;
11. final clean tree and runtime still OFF/stopped.

Any failure triggers a reset to the exact baseline and removal of patch-added files.
