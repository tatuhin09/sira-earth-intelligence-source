# Strategy Learner v1

Strategy Learner v1 is a local, advisory ranking layer over candidate strategies
that are already defined by SIRA's provider/policy/runtime subsystems.

It never makes a blocked or unavailable strategy eligible and cannot authorize
spending, credentials, protected-shell changes, or promotion.

Learning uses only real memory outcome events whose context explicitly binds a
`strategy_id` and `capability`. Synthetic-only memory is excluded. At least two
eligible events are required before a learned adjustment is applied.

Positive and negative historical outcomes produce bounded adjustments in
[-0.15, +0.15]. Contradicted memories are discounted rather than erased.
The decision object exposes aggregate counts/signals only; raw outcome context
is not returned.
