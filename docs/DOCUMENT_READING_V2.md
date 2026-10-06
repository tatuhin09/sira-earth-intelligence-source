# Document reading v2

Evidence (55 minute trial, 8 goal attempts, 0 verified claims): 16 failed page reads and 5 of
8 attempts with fewer than two readable documents, so the grounded step ran once.
Failing URLs: DOAJ article pages (HTTP 403), CISA PDFs, a w3.org URL answering 301, an arXiv
full-text HTML page. DOAJ outranked readable documentation hosts for the two source slots.

Changes
- `_fetch_public_document`: follows at most 2 redirects, only to the same host over plain
  https (no credentials, port or fragment); anything else still fails as before. The requested
  URL stays the document's identity.
- `web_search_discovery.normalize_lead_url`: arXiv full-text/PDF links become the abstract page;
  document downloads (PDF, archives, media) and non-https hits are dropped before they use a
  source slot. They are counted in `leads_dropped`. PDFs stay unsupported (the built-in PDF reader
  only handles simple files).
- `source_fetch_health.py`: a host that answers 401/403 three times is skipped for 7 days; a
  success clears it. SIRA never tries to evade a refusal. Applied to goal search results.
- General engine ranking: official (.gov/.edu) > owner trusted > scholarly > Wikipedia.
- Grounded step: passage selection needs one focus term instead of two. The claim gate that
  decides acceptance is unchanged.

Not changed: the 1 MB page limit, PDF support, the exact-sentence rule.
Unknown: why one Wikipedia page failed with invalid_source_content (URL was not recorded).

## PDF reading update

The v2 limits above describe that release. The shared reader now handles bounded, text-based PDFs
through Poppler's `pdftotext` when installed, with a small parser fallback for simple files.
Search discovery can retain PDF leads, the general learning readers can extract them, and the local
library can index `.pdf` files with page labels. Image-only/scanned PDFs still need OCR and report
`pdf_no_text`; encrypted, oversized, malformed and timed-out documents remain rejected.
