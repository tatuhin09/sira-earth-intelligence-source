# SIRA v1.2 Multi-Worker Foundation Design

## Goal

Introduce a bounded coordinator that can run independent read-only improvement workers concurrently while keeping code generation serialized, resource-budgeted, isolated from the main tree, and unable to promote anything directly.

## Architecture

The coordinator owns one structured task record under `improvements/workers/`. Each task gets distinct worker workspace and artifact directories. Research and local verification are allowed to run concurrently with a maximum concurrency of two. A future code-worker integration consumes their persisted results only after both phases finish and only when the existing global metered-resource budget allows it.

Promotion remains outside the coordinator. A local atomic promotion lease provides a single-owner concurrency primitive, but the existing Evaluator 2 -> Evaluator 1 -> transactional promotion/rollback chain remains authoritative. The coordinator cannot grant itself promotion authority.

## Worker roles

- `research`: read-only research/evidence preparation.
- `verification`: read-only local analysis/benchmark preparation.
- `code`: serialized candidate preparation after read-only workers complete.

The foundation does not make `self on` dispatch these roles yet. Runtime integration is a later v1.2 stage after the coordination layer is verified independently.

## Persistence and recovery

Every task has an atomic JSON task record. Worker results are stored as per-role artifacts. If SIRA restarts with a task still marked `running`, recovery marks it `abandoned_reselect`; it never resumes an unknown partial mutation. Existing artifacts are preserved for audit.

## Resource and safety constraints

- Maximum two concurrent read-only workers.
- Code work is serialized after the read-only phase.
- The existing metered budget checker can defer code work without failing the task.
- No worker receives promotion authority.
- A filesystem `O_EXCL` lease allows only one promotion owner at a time when later integrated.
- Protected-shell policy remains unchanged.
- Offline diagnostic performs zero external API/model calls and never enables paid spending.

## Success criteria

The foundation is accepted when tests prove: distinct workspaces, concurrent read-only execution, serialized code execution, budget deferral, single-owner promotion lease, crash-safe orphan recovery, and an offline CLI diagnostic with zero external/model requests.
