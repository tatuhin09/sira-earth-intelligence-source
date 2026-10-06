# Scholarly paper discovery (v0.6)

`python sira.py papers "research question"` performs bounded scholarly-metadata discovery.
The default `auto` mode is free-first:

1. Semantic Scholar relevance search (one fast attempt in auto mode).
2. Crossref public REST fallback when Semantic Scholar is unavailable, rate-limited, or empty.
3. One arXiv Atom query when the base result set is short or lacks abstracts/open-access PDF metadata.

The arXiv result set is merged with the base results by DOI or normalized exact title, missing
metadata is filled without replacing the primary paper identity, and the combined candidates are
ranked deterministically before SIRA keeps at most three.

This command discovers paper candidates. It is not evidence synthesis or factual verification.

## Bounds

- Maximum persisted results: 3.
- arXiv: at most one Atom request per paper run; no API key or paid service.
- Crossref: at most one bounded public REST request in the fallback chain.
- Semantic Scholar in `auto`: one fast attempt before failover.
- Timeout: existing `Settings.timeout_seconds` (default 20 seconds) per request.
- Response bounds: 2 MiB for JSON/Atom responses.
- Cache: 24 hours by default under `.cache/papers/`, keyed by full provider configuration.
- PDFs: never downloaded. arXiv and Semantic Scholar may contribute validated open-access PDF URLs.
- Models: no Gemini/OpenAI/other model call.
- Dependencies: Python standard library only.

arXiv asks API clients making repeated calls to wait between calls. SIRA v0.6 performs only one
arXiv request in a single `papers` run and relies on its 24-hour cache for repeated identical work.

## Commands

```bash
# Recommended free multi-provider mode
python sira.py papers "evidence grounded language models"

# Force one provider for diagnostics/comparison
python sira.py papers "evidence grounded language models" --provider semantic-scholar
python sira.py papers "evidence grounded language models" --provider crossref
python sira.py papers "evidence grounded language models" --provider arxiv

# Fresh provider request(s)
python sira.py papers "evidence grounded language models" --no-cache

# Offline structural regression suite
python sira.py benchmark --papers
```

## Stored paper fields

Each accepted paper contains:

- `paper_id`
- `title`
- `abstract` (nullable)
- `authors`
- `year` (nullable)
- `published_date` (`YYYY-MM-DD`, nullable)
- `doi` (nullable)
- `url`
- `open_access_pdf_url` (nullable)
- `retrieved_at`
- `provider` (the primary record identity/provider)
- `providers` (all providers that contributed to the final metadata)

When Crossref and arXiv identify the same work by DOI or normalized exact title, SIRA keeps the
primary record identity and fills only missing metadata from arXiv. That prevents enrichment from
silently changing the canonical record while preserving provenance.

## Metrics

Paper runs keep the v0.5 failover metrics and add:

- `enrichment_used`
- `papers_merged`
- `papers_enriched`

They also retain `providers_attempted`, `fallback_used`, `provider_failures`, total `api_requests`,
cache status, duplicate count, rejected rows, and metadata-availability counts.

## Failure behavior

- `completed`: at least one valid paper record exists.
- `no_results`: all completed providers yielded no acceptable record.
- `failed`: every useful provider path failed before a usable record was recovered.
- `cancelled`: user interruption.

If the base Semantic Scholar/Crossref chain fails but arXiv succeeds, the overall run is recovered
and completes. If arXiv enrichment fails while valid base papers already exist, those base papers
remain usable and the arXiv provider error is recorded in bounded diagnostics.

# Paper evidence integration (v0.7)

A completed `papers` run can now become a normal SIRA evidence run:

```bash
python sira.py paper-read <paper_run_id>
```

The reader prefers each paper's stored abstract, so a paper with a usable abstract needs no new
network request. If an abstract is missing and the paper metadata contains a validated
`open_access_pdf_url`, SIRA makes one bounded public PDF request and attempts conservative text
extraction in memory. Raw PDFs are not persisted.

The resulting run writes `evidence.json`, `report.md`, `report.html`, `documents/P1.txt` through
`P3.txt`, and `audit.jsonl`. It is cryptographically bound to the parent `papers.json` by SHA-256.
The existing command then consumes the result without a separate paper-specific answer engine:

```bash
python sira.py answer <paper_evidence_run_id>
```

## v0.7 PDF bounds

- public HTTP(S) URL only; local/private literal IPs and credential URLs are rejected;
- maximum response body: 8 MiB;
- maximum extracted text: 200,000 characters;
- maximum three PDFs per paper run;
- encrypted PDFs are rejected;
- unsupported or malformed PDFs fail per paper rather than aborting readable abstracts;
- only simple literal/hex text from supported content streams is extracted; this is intentionally
  conservative and is not a general-purpose PDF engine;
- no subprocess, browser, paid API, Gemini, or OpenAI call is required for `paper-read`.

Paper/abstract/PDF text is always treated as untrusted data. Text that says to ignore instructions,
execute commands, reveal secrets, or alter SIRA behavior is stored only as evidence text and is not
executed.

Offline regression:

```bash
python sira.py benchmark --paper-reading
```
