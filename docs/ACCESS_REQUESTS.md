# Access Request Foundation

This step adds a metadata-only owner access/permission queue for future SIRA
autonomy.

Behavior:
- blocked work records exactly what access is needed and why;
- the blocked task is referenced, not copied;
- `continue_other_work` is always true for pending requests;
- owner approval and actual access availability are separate states;
- satisfied requests become resume-ready exactly once;
- no secret values, passwords, tokens, card data, or session cookies are stored;
- payment approvals never become standing autonomous authority.

The queue is not wired into the scheduler yet. A later runtime step can reuse
the existing multi-worker scheduler and consume `resume_ready()` without
creating a second task system.

CLI examples:

```bash
python src/sira/access_requests.py --root ~/sira list
python src/sira/access_requests.py --root ~/sira notifications
python src/sira/access_requests.py --root ~/sira resume-ready
```

Create a missing credential request:

```bash
python src/sira/access_requests.py --root ~/sira create   --kind credential   --resource tavily   --provider tavily   --credential-name TAVILY_API_KEY   --reason "Web search is blocked because the required credential is unavailable."   --owner-action "Provision the credential through the approved secret channel."   --task-kind multi_worker   --task-id mw_example
```
