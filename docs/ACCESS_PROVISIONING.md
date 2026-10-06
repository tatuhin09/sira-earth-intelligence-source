# Verified Access Provisioning

Approved credential requests are no longer treated as satisfied merely because
an owner action was recorded.

For supported credentials, SIRA verifies local availability using the existing
literal secret loader in `config.py`. The value itself is never returned,
persisted into access metadata, copied into worker artifacts, included in model
context, or written to notification payloads.

Supported local credentials currently follow `config.SUPPORTED_CREDENTIALS`:

- `TAVILY_API_KEY`
- `GEMINI_API_KEY`
- `SIRA_SEMANTIC_SCHOLAR_API_KEY`

The detached autonomous worker deliberately does not inherit these variables
from its parent environment. Verification can still succeed from the main
project `.env`, which is the intended local secret source. Candidate workspaces
do not receive `.env`.

Owner-facing behavior:

```bash
python sira.py access approve ar_...
python sira.py access satisfy ar_...
python sira.py access reconcile
```

`access satisfy` now performs verification first. If the credential is absent
or invalid, the request remains `approved_waiting_provision`.

The autonomous cycle also reconciles approved credential requests before
selecting blocked targets. Once a credential is verified, the request becomes
`satisfied_resume_ready`; the existing fresh-reselection path can then consume
the resume once.

Non-credential access kinds are not automatically verified by this foundation.
Future permission/login/account verifiers should be explicit and type-specific,
not converted into blanket trust.
