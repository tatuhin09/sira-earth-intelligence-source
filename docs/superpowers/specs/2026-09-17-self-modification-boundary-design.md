# SIRA v1.0B Self-Modification Boundary Design

## Scope
v1.0B-1 establishes an isolated candidate workspace and a validated code-writer contract. It does not auto-promote, mutate the main tree, or use provider credentials.

## Modifiable surface
Future autonomous writers may modify text files under `src/sira/`, `tests/`, `benchmarks/`, and `docs/`, except protected shell paths. Operational files needed to run a candidate may be copied into the candidate workspace but are not writer-modifiable unless explicitly allowed later.

## Protected shell
The autonomous writer must reject edits to `src/sira/runtime.py`, `src/sira/self_modification.py`, and future protected gate/identity/audit/safety modules. Path traversal, absolute paths, symlink targets, binary payloads, and oversized writes are rejected.

## Workspace isolation
Candidate workspaces copy only an explicit public project allowlist. They never copy `.env`, memory, runtime state, improvements history, update backups, cache, runs, `.git`, or arbitrary repository files. A manifest records policy version, source digest, modifiable roots, protected paths, and candidate path.

## Promotion boundary
v1.0B-1 performs no promotion. Existing experiments may evaluate the candidate copy, but `promotion_performed` and `main_tree_modified` remain false. Automatic evaluator/promotion logic belongs to v1.0B-2/v1.0B-3.
