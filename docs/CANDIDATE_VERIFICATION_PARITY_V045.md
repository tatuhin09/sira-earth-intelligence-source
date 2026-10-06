# SIRA v0.4.5 — Candidate Verification Parity

The live writer check proved Gemini structured code generation was working, but
Evaluator 2 rejected a harmless diagnostic candidate because the isolated
candidate workspace did not contain all public files required by the full test
suite.

The candidate copy allowlist predated the Desktop UI and repository-hygiene
tests. It omitted:
- desktop/
- tools/
- .gitignore

These paths are now copied into isolated candidates only as verification
support. They are NOT added to MODIFIABLE_ROOTS, so autonomous writers cannot
edit them through ValidatedCandidateEditor.

The public tree digest also binds these support files, preserving stale-main and
candidate-integrity checks.

The installer performs an offline real-repository candidate parity check:
prepare candidate -> add only the writer diagnostic docs marker -> run the full
unittest suite inside that candidate. No live provider call is made.
