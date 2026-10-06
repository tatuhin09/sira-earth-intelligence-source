# SIRA v0.7 Paper Evidence Integration Design

## Purpose

Connect scholarly-paper discovery to SIRA's existing provenance-checked evidence and verified synthesis pipeline without creating a second answer engine or adding a paid dependency.

## Data flow

`papers <query>` produces `papers.json`. `paper-read <paper_run_id>` validates and hashes that parent artifact, then converts up to three papers into a schema-version-1 `source_evidence` run. Existing `answer <evidence_run_id>` consumes the resulting evidence unchanged.

## Reading policy

1. Prefer a paper's stored abstract. This requires no network call and preserves the paper-provider retrieval timestamp.
2. If no usable abstract exists and `open_access_pdf_url` is present, fetch only that public HTTP(S) URL through a bounded PDF reader.
3. Raw PDF bytes stay in memory and are never persisted. Only extracted UTF-8 text is stored.
4. One PDF failure is recorded on that paper and does not discard other readable papers.

## PDF safety bounds

- maximum three URLs per paper run;
- default request timeout inherited from `Settings`;
- maximum PDF response body 8 MiB;
- maximum extracted text 200,000 characters;
- public URL checks reject credentials, unsupported ports, local names and literal non-global IPs;
- encrypted PDFs fail closed;
- unsupported filters or unextractable content fail per paper;
- only conservative literal/hex text inside supported PDF content streams is extracted;
- no subprocess, shell command, browser automation or third-party PDF dependency.

## Provenance

Paper evidence uses source IDs `P1` through `P3`. Every readable document stores SHA-256, exact text offsets for extracted evidence quotes, the paper identity, DOI/provider metadata, content origin (`paper_abstract` or `open_access_pdf`) and the actual evidence URL. The evidence run stores `parent_artifact: papers.json` and the SHA-256 of that exact parent file. Existing evidence loading re-checks both parent and document hashes before synthesis.

## Trust boundary

Paper text is untrusted data. Instructions contained in abstracts or PDF text are never executed. Reports HTML-escape source text. Claim acceptance remains the responsibility of the existing proposal/verifier/local-gate synthesis pipeline.

## Caching and metrics

PDF text is cached for 24 hours using reader namespace + canonical PDF URL. Metrics distinguish abstracts used, PDFs used, cache hits, API requests, failures and quote integrity. Abstract-only runs use zero network/API requests.

## Regression requirements

- Existing web evidence remains compatible.
- Existing title/question bounds remain unchanged.
- Existing synthesis tests must pass.
- Paper-reading benchmark is fully offline and makes zero API requests.
- No paid provider is required.
