# SIRA Desktop v0.4 — Explicit Verified Research from Chat

v0.4 adds an explicit research path to the Desktop UI without changing SIRA's
autonomous self-improvement runtime or giving normal chat hidden browsing.

## Explicit-only rule

Research starts only when:

- the user presses the Research button; or
- the message contains a clear research/verification command such as
  `Research ...`, `Verify ...`, `রিসার্চ করো`, or `যাচাই করো`.

Ordinary conversation never silently starts external research.

## Free-first routing

- Scholarly/academic research uses the existing free-first `papers` command:
  Semantic Scholar → Crossref → arXiv fallback.
- Biomedical research uses the existing free biomedical mesh:
  Europe PMC / PubMed.
- General/current-web research first records the free-only Research Mesh plan.
  When no executable free general-web route is available, Gemini Search
  grounding is allowed only after a separate owner confirmation that Search is
  intended for zero-cost/no-charge use.

The Search confirmation is only a local SIRA safety gate. It does not modify
Google billing and cannot guarantee external provider billing behavior.

## Verification

Scholarly/biomedical research is marked verified only when at least two
distinct evidence records are available.

General web research is marked verified only when grounded sources include at
least two distinct web hosts.

If evidence does not meet the threshold, SIRA reports `limited_evidence`
instead of pretending the answer is verified.

## UI

Research jobs expose:

- queued/running/completed/limited/blocked/failed status;
- current progress phase and percentage;
- selected route;
- verification status;
- source/evidence cards;
- API/model request counts;
- final source-grounded answer;
- local persisted job history.

## Authority

Explicit research has no authority to start/stop the autonomous runtime,
promote code, enable billing, authorize paid spending, install packages, or
modify protected code.
