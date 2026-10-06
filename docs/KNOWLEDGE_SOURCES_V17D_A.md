# SIRA v1.7D-A — Free Knowledge Source Foundation

Adds two separate public/free, read-only capabilities:

- `knowledge_search` -> Wikimedia page-search metadata
- `open_access_search` -> DOAJ article metadata

DOAJ is deliberately not added to generic `paper_search`, so existing paper
consumers cannot accidentally receive an unsupported provider.

Both paths reuse multilingual query preparation, local caching, provenance,
and the Capability Broker/Research Mesh plan. They grant no authority,
perform no billing action, and cannot authorize promotion.

v1.7D-A is acquisition only. Durable factual consolidation, source agreement,
contradiction handling, and freshness/revalidation belong to v1.7D-B/C.
