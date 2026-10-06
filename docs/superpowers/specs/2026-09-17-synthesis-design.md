# Evidence verification and answer synthesis v0.3

The approved next Stage 1 slice turns a completed `source_evidence` run into a
cited answer while preserving all prior artifacts. The user requested a robust
foundation that will not need replacement when later model, RAG, memory and
evaluation layers arrive, while avoiding unnecessary local compute.

## Architecture

`answer EVIDENCE_RUN_ID` validates the immutable parent evidence file and every
stored document hash, then runs a provider-neutral `Retriever` over those local
documents. The first retriever is deterministic lexical passage ranking: bounded
paragraph chunks retain source IDs, URLs, character offsets and content hashes.
Its interface can later be replaced by embeddings/vector search without changing
the synthesis controller or answer schema.

A provider-neutral `SynthesisModel` performs two independent calls. Proposal
creates atomic claims and cites passage IDs. Verification receives the question,
claims and same evidence packets, then labels each claim supported, mixed or
unsupported. The local acceptance gate rejects malformed IDs, unknown passages,
missing supports, verifier disagreement and any claim not marked supported.
Final Markdown/HTML is deterministically rendered only from accepted claims;
the model cannot insert uncited prose after verification.

The live initial provider is Google's `gemini-3.1-flash-lite` through the
GenerateContent REST endpoint with JSON Schema structured output. Official docs
checked 2026-09-17: stable model code, structured outputs supported, free-tier
pricing available. The client uses two bounded calls, no tools/search/code
execution, no automatic retry, temperature 0, fixed token cap and API key header.
Free tier is an account property and user must keep billing disabled.

## Trust, metrics and artifacts

Web text stays untrusted JSON data inside explicit delimiters. Prompt instructions
inside evidence have no tool path. Model output is untrusted until schema and
reference validation. The same model performing both passes is recorded as
`same_model_second_pass`, never called independent verification. Factual accuracy
and objective citation correctness remain null until the protected evaluator.

Each answer run stores parent ID/hash, document hashes, selected passages,
proposal, verdict, accepted/rejected claims, usage, latency, code hash, prompt
versions, model ID, `answer.json`, `answer.md`, escaped `answer.html`, and audit
events. Measurable metrics include structural citation coverage/resolution,
model-verifier acceptance, supporting source count/diversity proxy, retrieval
packet count, API calls/tokens and cache hits. Candidate and verdict responses are
cached by input hash/model/prompt version for 24 hours; final runs remain unique.

Offline fixtures test retrieval relevance, exact offsets, prompt injection staying
data, contradictory/unsupported claim removal, malformed model responses, parent
tampering, cache behavior, rendering and metrics. A fixed synthesis benchmark
measures expected accepted/rejected claims without network and is explicitly not
a live factual-accuracy benchmark.

## Boundaries

At most five read documents, 200,000 characters each, twelve selected passages,
12,000 evidence characters, twelve proposed claims, 1,000 characters per claim,
two model calls per uncached answer, 30-second socket timeout and 2 MiB response
cap. No autonomous loop, memory promotion, cloud database, model training,
self-modification or independent final evaluator is added in this slice.

