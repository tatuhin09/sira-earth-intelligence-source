# SIRA v1.0D Unified Autonomous Loop Design

## Scope

This design unifies autonomous target selection so persistent self-improvement can prioritize real failures before proactive opportunities. v1.0D-1 implements only the target scheduler and opportunity lineage/diminishing-return guard. It does not change the persistent `self on` worker yet.

## Priority contract

- Priority class 0: deterministic code/test or benchmark regressions from real learning memory.
- Priority class 1: other eligible unresolved failure/rejection memories.
- Priority class 2: proactive local code opportunities.
- Existing per-source scores remain the ordering signal inside each class; a proactive opportunity can never outrank an eligible failure/rejection solely because its static complexity score is larger.
- Synthetic fixture/mock/dummy learning memories remain excluded by the existing memory hygiene policy.

## Opportunity lineage contract

- Opportunity fingerprints remain source-sensitive identities for exact-attempt cooldown.
- A separate stable lineage key is derived from opportunity type + path + symbol and survives source-hash changes.
- Successful promotions create lineage history for that stable target.
- A target successfully promoted within the last 24 hours is temporarily suppressed even if its source fingerprint changed.
- After the 24-hour suppression expires, recent successful promotions apply a bounded diminishing-return priority penalty so repeated refactoring is deprioritized rather than permanently banned.
- Existing v1.0C-3d handoff artifacts are imported into lineage history so already-completed promotions are recognized immediately after this update.
- Lineage state is bounded, auditable, local-only, and contains no credentials.

## Resource and safety contract

- Target selection performs zero network/model calls and has `paid_spending=false`.
- Protected shell remains excluded from proactive opportunity scanning.
- v1.0D-1 does not execute research, Gemini, candidate edits, evaluators, promotion, or rollback.
- Runtime worker integration is deferred to the next v1.0D stage.
