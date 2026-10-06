# Gap-driven learning goals

Evidence: the autonomous loop was idle in 89.6% of target selections (3754 of 4191) and
every learning goal had run out of fresh study steps (4 steps per goal, retried with the
same outcome). The self-model already computes capability gaps but nothing consumed them.

What: `src/sira/gap_goal_generator.py`, called from `select_autonomous_target` (default
goal provider only). When the owner enables it, a self-model gap with no active goal becomes
a public-research learning goal with a short study plan (curated table `GAP_TOPICS`, one
topic per self-model capability). It is a no-op unless `memory/gap_goals/policy.json` exists.

Bounds: <= `max_active` (1..5, default 2) generated goals active at once; one new goal per
check; a check at most every 15 minutes; per-capability cooldown (default 14 days);
exhausted goals (every step attempted twice) are paused to free a slot; owner goals and
topics are never touched; no model call, no paid request, no authority, no promotion.

Audit trail: `memory/gap_goals/registry.json` stores, per goal, the capability, gap reason,
evidence references and creation time (why SIRA created it).

Owner control: `python tools/sira_gap_goals.py enable|disable|status`.
Limit: the topic table is curated (SIRA-relevant engineering knowledge). Free-form topic
invention by SIRA is a later step. Verified claims from grounded verification are not yet
counted as self-model research evidence on purpose, until their precision is measured.
