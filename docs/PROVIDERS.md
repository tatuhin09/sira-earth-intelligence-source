# Provider contract checked 2026-09-17

Only Tavily is integrated in Stage 1 so far. Official references:

- [Credits and pricing](https://docs.tavily.com/documentation/api-credits):
  free plan currently offers 1,000 credits/month without a credit card; basic
  search costs one credit and advanced search two. Free plan does not mean
  unlimited usage. Keep PAYGO/paid billing disabled in the dashboard.
- [Search endpoint](https://docs.tavily.com/documentation/api-reference/endpoint/search):
  POST `https://api.tavily.com/search`, Bearer key in Authorization header.
  We fix basic/general search, disable auto_parameters, generated answer and raw
  content, limit result count and request usage. Returned URL/content fields are
  validated as data. Unknown credit usage remains null.
- [Rate limits](https://docs.tavily.com/documentation/rate-limits): development
  keys currently allow 100 requests/minute. Production keys require a paid plan
  or PAYGO. This starter is sequential, performs one request per cache miss and
  does not auto-retry. A 429 includes Retry-After when available.
- [Quickstart](https://docs.tavily.com/documentation/quickstart): sign in at
  [the dashboard](https://app.tavily.com/) and obtain an API key.

Recheck documentation before changing provider options or relying on these limits
at a later date. No API policy was assumed for Semantic Scholar or GitHub; those
providers are deliberately outside this first small implementation slice.

This application cannot tell whether the user's key belongs to a free or paid
account. Explicit basic search bounds per-request work; it does not enforce an
account-level spending limit. No billing changes are performed by this client.

## v0.2 document extraction

[Official Extract contract](https://docs.tavily.com/documentation/api-reference/endpoint/extract)
rechecked 2026-09-17: POST `/extract`, Basic depth, text format, 10-second service
timeout and usage reporting. SIRA sends at most five URLs and does not request
query-reranked chunks. Both successful and failed arrays must be checked; response
order is not guaranteed. Results are associated by exact canonical URL. A changed
or unrequested result URL is not silently attributed to a requested source.

Basic extraction costs one credit per five successful extractions. Reported usage
can be zero before the provider's billing count reaches five; zero is not evidence
that extraction is always free. SIRA records the reported value or null if absent.
It never upgrades to advanced extraction or retries automatically.

Local client requests only the fixed Tavily API endpoint. The external service
performs page fetching and its own network/access controls. SIRA rejects obvious
private/local/credential-bearing URL inputs but does not independently resolve
target hostnames or certify the provider's internal fetch behavior. Neither
successful extraction nor quote matching establishes the truth of a claim.

## v0.3 Gemini synthesis

Official Google documentation rechecked 2026-09-17:

- [Gemini 3.1 Flash-Lite model](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite)
  identifies stable model code `gemini-3.1-flash-lite` and structured-output
  support. SIRA pins that exact model rather than a moving `latest` alias.
- [Structured outputs](https://ai.google.dev/gemini-api/docs/structured-output)
  documents JSON Schema controlled responses. SIRA still validates every parsed
  field locally because schema-shaped output is not evidence of truth.
- [Text generation](https://ai.google.dev/gemini-api/docs/text-generation)
  documents the `generateContent` REST method and `x-goog-api-key` header.
- [Pricing](https://ai.google.dev/gemini-api/docs/pricing) lists a free tier for
  eligible Gemini API use. [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
  explains that active limits depend on project/tier and are visible in AI Studio.

SIRA performs at most two sequential calls per uncached answer, never auto-retries,
sets temperature zero, requests structured JSON, and provides no model tools,
search, code execution or terminal. It records token counts when Google returns
them. The client cannot prove that a key is free-tier or prevent account-level
billing, so keep billing disabled and monitor the project in AI Studio.

## v0.4 Semantic Scholar paper discovery

Official Semantic Scholar documentation rechecked 2026-09-17:

- Academic Graph base API: `https://api.semanticscholar.org/graph/v1`.
- Paper relevance search: `GET /paper/search` with plain-text `query`, bounded `limit`,
  and explicit `fields`. SIRA asks only for `title,abstract,authors,year,url,externalIds,openAccessPdf`.
- Most Semantic Scholar endpoints are publicly available without authentication. Their
  official API overview says unauthenticated traffic shares a public rate limit and may
  be further throttled during heavy use; an API key is recommended for higher/predictable
  limits but is not required by this integration.
- Semantic Scholar documents that hyphenated search terms can fail to match. SIRA
  replaces ASCII hyphens with spaces only in the provider query while preserving the
  original normalized question in the run artifact.
- The API exposes open-access PDF metadata. SIRA stores only a validated URL and does
  not download the PDF in this stage.

SIRA performs one anonymous GET per uncached paper run, returns at most three records,
does not automatically retry `429` or other errors, and uses the same bounded JSON
transport as other providers. This client performs no billing action and requires no
paid service. Public availability is not a guarantee against throttling or future API
policy changes, so recheck official documentation before increasing request volume.


## Semantic Scholar resilience (v0.4.1)

Paper search remains anonymous by default. SIRA may optionally send a configured
`SIRA_SEMANTIC_SCHOLAR_API_KEY` as `x-api-key`. HTTP 429 is the only paper-search
transport status retried automatically: at most three total attempts, short deterministic
backoff, and no sleep longer than five seconds. Final exhaustion is stored as
`rate_limited`; actual attempts are reflected in `api_requests`.

## v0.5 free multi-provider paper fallback

Paper discovery now has a provider-neutral failover layer. Default `papers` mode uses:

1. Semantic Scholar Academic Graph (`paper/search`) with a single fast attempt.
2. Crossref public REST `works` bibliographic query if Semantic Scholar is unavailable,
   rate-limited, or yields no usable papers.

Crossref is used only through its free public metadata API; SIRA v0.5 has no Metadata
Plus token, paid fallback, or hidden billing path. The Crossref request asks for at
most three rows and selects only the metadata needed by SIRA. Crossref full-text/TDM
links are not treated as proof of open access, so they are not copied into SIRA's
`open_access_pdf_url` field.

`FallbackPaperProvider` is intentionally provider-neutral. New free scholarly metadata
providers can be inserted later without changing `papers_run` or its persisted artifact
schema. Provider attempts, recovered failures, and failover use are persisted as bounded
metrics for later evaluator/failure-learning work.

## v0.6 arXiv metadata enrichment

Official arXiv API documentation rechecked 2026-09-17:

- User manual: https://info.arxiv.org/help/api/user-manual.html
- The query interface accepts `search_query`, `start`, and `max_results`, with optional relevance
  sorting, and returns Atom 1.0 XML entries containing title, summary/abstract, authors, dates,
  links and arXiv extension metadata such as DOI when available.
- arXiv documents `all:` field search and Boolean `AND`; SIRA converts the user's bounded question
  into provider-owned `all:term AND ...` syntax so user punctuation cannot become query operators.
- arXiv recommends a delay when an application makes multiple API calls in a row. SIRA v0.6 makes
  at most one arXiv request in a single paper run and uses the existing 24-hour paper cache.

SIRA parses Atom with the Python standard library, caps responses at 2 MiB, rejects DTD/entity
payloads before parsing, validates arXiv identifiers, and stores canonical HTTPS abstract/PDF URLs.
No API key, paid plan, PDF download, or model call is introduced by this provider.

## Opportunity research fallback and relevance gate (v1.0C-3c2)

Evidence-backed opportunity research uses bounded public scholarly metadata in this order: arXiv, Crossref, then anonymous OpenAlex. OpenAlex is queried without an API key or billing credential in this stage. Transient provider failures (429, 502/503/504, network/timeout) enter a local cooldown so repeated autonomous scans do not hammer an unavailable public service. Duplicate works are merged by DOI/title so a richer OpenAlex abstract can enrich sparse Crossref metadata while retaining provider provenance.

Before external evidence can authorize a writer handoff, SIRA now scores each citation locally against target-specific engineering signals derived from the opportunity type. Unrelated sources are excluded from the writer citation set and retained only as bounded audit metadata. The quality gate counts only relevant sources and relevant abstracts, so provider/source-count thresholds cannot be satisfied by semantically unrelated papers. This filter is deterministic and makes no metered model request.
