# Access-aware Runtime Integration

This step connects the existing access-request foundation to SIRA's existing
target selector, multi-worker coordinator and persistent runtime. It does not
create a second scheduler.

When opportunity research reports an explicit credential/authentication access
failure, the code phase becomes `deferred_owner_access`, a sanitized owner
request is created, and the target is parked at autonomous-target identity
level.

Pending and approved-but-not-yet-satisfied requests exclude that target from
later target selection, allowing other eligible work to continue. After the
request is marked satisfied, the target becomes eligible again. When selected,
its resume signal is consumed.

Resume is fresh reselection, not continuation of a half-completed worker
transaction. This matches SIRA's existing crash-recovery rule.

Transient network/rate-limit failures are not owner-access requests. They remain
under provider cooldown and runtime reliability handling.

This step does not execute payments, bypass external security controls, or grant
itself permissions.
