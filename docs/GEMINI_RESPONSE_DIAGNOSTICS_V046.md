# SIRA v0.4.6 — Gemini Response Diagnostics

Production autonomous cycles continued to report the generic provider code
`invalid_response` on the complex memory.py::retrieve opportunity even though
the guarded writer live-check succeeded.

This patch changes only sanitized failure classification in the Gemini code
provider. It never persists raw model output, finish messages, prompts, or
secrets.

New bounded provider codes:
- missing_candidates
- invalid_candidate_shape
- missing_content
- missing_parts
- empty_response_text
- malformed_json
- json_not_object
- response_recitation
- unsupported_language
- generation_stopped_other
- malformed_function_call
- generation_stopped_unknown

Existing explicit codes such as output_truncated and response_blocked remain.

No retry count, runtime authority, promotion authority, payment authority,
candidate validation, evaluator behavior, or protected boundary is expanded.
The installer makes no live provider call.


## R2 compatibility update
The older v0.4.4 malformed-response regression now expects `malformed_json`
instead of the superseded generic `invalid_response` code. Request-count
semantics are unchanged.
