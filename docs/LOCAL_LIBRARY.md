# Local Library v1.1 (large read-only document store)

Purpose: give SIRA a big, fast, offline source of readable material (official docs, standards,
papers, books, notes) so learning is not limited by web rate limits, blocked hosts or PDF/redirect
problems seen in the trials. Fits the A81 architecture: RAW/TRUSTED CORPUS -> candidate knowledge
-> VERIFIED MEMORY. Library text is never verified memory by itself.

## Memory tiers (what lives where)
| Tier | What | Where | Trust |
|---|---|---|---|
| Raw library | owner-supplied files (tens of GB) | `<library>/collections/<collection>/<source>/` on the big volume | unverified, read-only |
| Search index | chunked text + FTS5 (BM25) + provenance | `<library>/index/library.sqlite3` (same volume) | derived, rebuildable |
| Collection trust | owner label per collection | `collection.json` in the collection folder | metadata only |
| Verified knowledge | small, strict, evidence-gated claims | existing `memory/` knowledge store | verified (>= 2 independent sources) |
| Experience / failure | what SIRA tried and why it failed | existing handoffs, reports, breaker | records |
| Working/hot | recent answers and caches | existing caches | derived |

A `<source>` folder is one independent origin (for example `python-docs`, `rfc-editor`).
Two documents corroborate a claim only if they come from different sources.

## Owner tool
`python tools/sira_library.py init --path /mnt/BIGDISK/sira_library`; copy files in; `set-trust`;
`sync` (scan + index, resumable, time/size bounded); `status`; `search`; `verify`.
Supported files: .txt .md .rst .markdown .html .htm .xhtml and text-based .pdf.
PDF extraction uses Poppler's `pdftotext` when available and retains page labels. A small
dependency-free reader is a fallback for simple PDFs. Input and extracted text are bounded;
encrypted, malformed, timed-out and oversized files are skipped with a reason. Image-only or
scanned PDFs report `pdf_no_text`; OCR is not included yet.

## Safety
No network, model or execution. Symlinks, hidden entries and special files are not followed; the
library must be outside the repository; files that look like they contain keys/passwords are skipped
and counted; size/depth/disk-space bounds; index text is data, never instructions.

## Measured (synthetic, 2500 HTML files = 52 MB, sandbox CPU)
Indexing about 3.7 MB/s raw; database 1.3-1.7x the extracted text size; search about 7 ms.
For 40-50 GB of raw files this suggests several hours of indexing (resumable) and a database of
roughly 1.3x the extracted text, which can be far smaller than the raw size for HTML-heavy data.
These numbers are NOT measured on your hardware or data.

## Not in v1.1
OCR for image-only PDFs, EPUB extraction, embeddings/semantic search, automatic downloads,
de-duplication across collections, and automatic local-library retrieval in learning goals.
