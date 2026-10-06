# SIRA Research Mesh v1.7

Research Mesh is a free-first, capability-brokered research layer.

## v1.7A

v1.7A adds two actual public biomedical literature adapters:

- Europe PMC: public REST search, core metadata and abstracts.
- PubMed/NCBI E-utilities: public search/summary metadata with local throttling.
  `SIRA_NCBI_API_KEY` is optional and increases the supported NCBI request rate.
  `SIRA_NCBI_EMAIL` may be supplied through the process environment.

The biomedical mesh never enables paid spending.

## Existing lanes

- scholarly: arXiv, Crossref, OpenAlex, Semantic Scholar
- software: GitHub Public
- web/document extraction: Tavily, policy-gated
- model synthesis/code: Gemini, policy/budget-gated

## Gemini web tools

The Gemini catalog now explicitly advertises:

- `google_search_grounding`
- `url_context`

These are model-tool capabilities and remain metered-risk lanes. They are not
automatically enabled by the free-first mesh. v1.7B will integrate the grounded
response contract after reusing the existing Gemini transport safely.

## Authority

Research Mesh cannot grant promotion authority, create access requests, or
enable paid usage. Evidence still flows through SIRA's existing verification,
memory, evaluator, and promotion boundaries.
