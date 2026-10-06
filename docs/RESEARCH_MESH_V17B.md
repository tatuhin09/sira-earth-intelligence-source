# SIRA Research Mesh v1.7B — Gemini Research Tools

v1.7B adds a separate bounded Gemini research-tool transport. Existing Gemini
structured synthesis and code-writing transports remain tool-free.

Supported transport modes:

- URL Context
- Google Search grounding
- combined URL Context + Google Search

The response parser preserves bounded grounding sources, web search queries,
grounding-support spans, URL retrieval statuses, and token usage.

## Zero-cost-first execution

Actual tool execution is fail-closed by default.

URL Context requires a local owner confirmation:

- `SIRA_GEMINI_FREE_TIER_CONFIRMED=true`

Google Search additionally requires:

- `SIRA_GEMINI_SEARCH_ZERO_COST_CONFIRMED=true`

and is constrained by a local monthly SIRA request cap. The local ledger cannot
observe requests performed outside SIRA or guarantee an external provider quota.

These confirmations do not authorize payment, billing setup, subscription
changes, quota purchases, or paid overage. SIRA never changes billing state.

## Important external limitation

Google's current Gemini pricing can differ by model/project/tier. The local SIRA
guard is intentionally conservative and cannot prove external billing status.
If zero-cost status is uncertain, execution remains blocked and SIRA should use
other free Research Mesh providers instead.

## Security

Retrieved pages/search results are treated as untrusted data. They cannot
override system instructions, request secrets, authorize code execution, or
grant promotion authority.
