# SIRA v1.0C-3 Opportunity Discovery Design

## Scope

This design adds autonomous improvement targets that do not depend on an existing failure memory. The work is staged. v1.0C-3a is local-only discovery and ranking; later stages may enrich a selected opportunity with free research and hand it to the existing writer/evaluator/promotion pipeline.

## v1.0C-3a contract

- Scan only local public/modifiable Python source under `src/sira`.
- Never scan credentials, runtime state, memory databases, Git metadata, or protected-shell source as candidate targets.
- Generate bounded, evidence-backed opportunities from deterministic static signals such as large modules, complex functions, parse failures, and explicit TODO/FIXME markers.
- Rank opportunities deterministically and expose the evidence and score used for ranking.
- Fingerprint each opportunity from its type, path, symbol, source hash, and evidence so a code change creates a fresh identity.
- Persist attempt history separately from scans. Merely viewing/scanning an opportunity must not start cooldown; an autonomous attempt does.
- Suppress recently attempted identical fingerprints for a bounded cooldown, while allowing a changed source fingerprint to become eligible immediately.
- Discovery must make zero network/API requests and must not modify the main source tree.
- Persist a bounded audit artifact for each discovery run.

## Resource policy

v1.0C-3a is local-only and costs zero API requests. Later research stages should prefer local/free sources before metered model calls. External free providers remain subject to their rate limits and terms; paid spending stays disabled unless the owner explicitly enables it.

## Integration boundary

This stage does not modify the persistent `self on` loop. A later v1.0C-3 stage will claim an eligible opportunity, perform research, and route any generated candidate through the already protected Evaluator 2 -> Evaluator 1 -> transactional promotion/rollback chain.

## v1.0C-3b evidence-enrichment contract

- Resolve an opportunity only from a persisted discovery artifact and reject it if the source hash is stale.
- Extract the exact target source slice, local callers, and existing tests without network/model calls.
- Map only known source modules to existing deterministic benchmark suites; unknown mappings remain ineligible for writer handoff.
- Read related learning memory only when a local memory database already exists, and exclude synthetic fixture/mock/dummy memory using the existing autonomous-memory hygiene rule.
- A static opportunity signal alone is insufficient. `research_ready` requires a deterministic benchmark mapping and an existing test reference, plus a bounded evidence score.
- Produce measurable success criteria: all unit tests pass, test count does not decrease, the relevant benchmark passes, protected shell remains unchanged, and the structural signal improves from its measured baseline.
- Persist an immutable-style JSON evidence artifact under `improvements/opportunities/evidence/`.
- Evidence enrichment makes zero API requests, spends no money, does not modify source, and does not start opportunity cooldown.
