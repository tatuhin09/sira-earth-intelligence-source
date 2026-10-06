# SIRA Desktop v0.4.1 — Scholarly Artifact Adapter Fix

Fixes the Desktop scholarly-research adapter. The CLI returns `papers` as a numeric count and points to the canonical `runs/<run_id>/papers.json` artifact. v0.4 incorrectly treated the CLI summary as if it contained the paper-record array. v0.4.1 validates the run id/path/schema/count/status and loads the canonical artifact before building source cards and verification evidence. No provider, authority, budget or runtime policy is expanded.
