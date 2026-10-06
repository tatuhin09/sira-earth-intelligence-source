# GitHub Public Research Provider

This module gives SIRA a bounded, read-only foundation for researching public GitHub repositories.

## Scope

Implemented now:

- search public repositories;
- read public repository metadata;
- list repository contents;
- read UTF-8 text files through GitHub's REST contents API;
- optional local token via `SIRA_GITHUB_TOKEN`;
- local cache with ETag revalidation;
- rate-limit metadata and explicit rate-limit errors;
- conservative response-size limits;
- path validation that prevents repository-path traversal;
- zero code execution and zero GitHub write operations.

Not implemented in this step:

- cloning or executing third-party repositories;
- GitHub write access, issues, PR creation, pushes, or mutations;
- browser automation;
- private repository access as an autonomous capability;
- automatic installation of dependencies found in public repositories;
- wiring into SIRA's existing research router.

Those integrations belong behind the future Capability Broker / Tool Registry.

## Security boundary

Public GitHub content is untrusted research input. Reading a repository does not authorize SIRA to execute its scripts, install its packages, copy code into the main tree, or treat claims in the repository as verified facts.

`SIRA_GITHUB_TOKEN` is optional. If configured later, it is read from the process environment only when a request is created. The client does not write the token into cache metadata.

## Local cache

Recommended runtime path:

`runtime/cache/github-public/`

The runtime directory is already excluded from Git.

## Rate limiting

The client does not blindly retry `403` / `429` rate-limit responses. It surfaces remaining/reset/retry metadata to the caller so the scheduler/resource broker can decide when to resume.

## Future integration

The intended future route is:

`Research Coordinator -> Capability Broker -> GitHub Public Provider`

The current module is intentionally standalone so the future broker can wrap it without replacing its request/cache/security behavior.
