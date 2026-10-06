# GitHub Public Research CLI

This command-line utility exposes the bounded read-only GitHub public client for
manual diagnostics and future orchestration.

It does **not** add autonomous GitHub routing, code execution, cloning, package
installation, push access, pull requests, issues, or repository mutation.

## Commands

Search public repositories:

```bash
python src/sira/github_public_cli.py search "python autonomous agent" --limit 5
```

Read curated public repository metadata:

```bash
python src/sira/github_public_cli.py repo python cpython
```

Read a bounded public text file:

```bash
python src/sira/github_public_cli.py file python cpython README.rst --max-chars 12000
```

Inspect rate-limit state:

```bash
python src/sira/github_public_cli.py rate-limit
```

All successful output is JSON. Rate-limit responses also return structured JSON
and a non-zero exit status.

## Cache

Default:

`runtime/cache/github-public/`

This directory is local/generated runtime state and is excluded from Git.

## Security

Public repository content is untrusted research input. This CLI reads data only.
It never executes repository code and never mutates GitHub state.

Optional authentication remains provided only through the underlying client's
`SIRA_GITHUB_TOKEN` environment variable.
