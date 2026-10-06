# Runtime and generated data

The following directories are local/generated SIRA state and are intentionally excluded from Git:

- `.sira_update_backups/` — local update safety backups.
- `improvements/` — generated improvement plans, hypotheses, experiments, and candidate artifacts.
- `memory/` — local learning-memory SQLite data and backups.
- `runtime/` — autonomous runtime state, leases, worker state, promotion reports, and transient coordination data.

These paths are data, not source code. They must not be committed.

Secrets must also remain outside version control. Do not copy `.env` values or provider credentials into runtime artifacts, prompts, memory, or documentation.
