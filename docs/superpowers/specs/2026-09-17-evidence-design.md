# Stage 1 evidence reading, v0.2

Continue the approved research architecture after the user's live search/cache
verification. User prefers appropriately complete work, avoiding disposable
implementations and unnecessary compute/tool use.

Use the cloud-first Tavily Basic Extract endpoint, not a local browser/scraper.
Official extract documentation rechecked 2026-09-17. Read an existing run without
another search. Up to five URLs in one bounded API batch; text output, no query
reranking (preserve the available extracted document), no retry or API redirect.
Per-document size and response caps apply. This is provider-extracted text, not
an independently verified copy of the original webpage or factual validation.

New ExtractProvider protocol separates reading from synthesis. Each new reading
run preserves parent ID, parent result hash, code hash, provider configuration,
UTC retrieval timestamps, untrusted text, content hashes, per-source outcomes,
and exact quote character offsets. JSON, Markdown and escaped static HTML reports
are generated together. No LLM, vector database or cloud storage account is needed.
These document/evidence records are a future knowledge-base ingestion format;
actual cloud storage and RAG are later separate implementation tasks.

Cache successful documents by reader version and canonical URL for 24 hours;
failed reads are not cached. Report credit usage as returned, including zero or
unknown; zero is not a blanket promise of free extraction because provider billing
may aggregate successful extractions. Missing/failed/unrequested/malformed items
must not attach text to a different source. Invalid local/private-looking URLs
are filtered before submitting; the client never connects to supplied URLs.

The parent run is never overwritten. Commands: `read RUN_ID [--no-cache]`,
`benchmark --reading`. Evidence generation is deterministic extractive quoting,
not a synthesized answer. Factual accuracy, semantic citation correctness and
coverage remain null. Quote integrity and source-read success are measured
separately. Rendering must not execute page instructions, HTML or markdown.

Changes: add reading.py (document contract/orchestration), reports.py (quote
selection/rendering), providers/tavily_extract.py (cloud adapter), shared bounded
JSON transport, reading_benchmark.py plus fixtures/tests; extend CLI. Extract
shared HTTP handling only as necessary to avoid divergent error/security behavior.

Deliver a hash-guarded updater: validate every target before writing, preserve
unrelated files and .env/runs/cache, back up changed paths, support idempotent
reapplication, reject modified targets/symlinks. User runs tests then Git commit.
