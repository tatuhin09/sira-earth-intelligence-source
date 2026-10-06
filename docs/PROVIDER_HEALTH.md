# Provider Health Snapshot

`src/sira/provider_health.py` composes existing SIRA provider state into one
read-only local snapshot.

It intentionally keeps four signals separate:

1. **Catalog** — what the provider can do.
2. **Readiness / policy** — whether credentials and resource policy allow it.
3. **Provider cooldown** — a provider-specific transient cooldown already
   persisted by opportunity research.
4. **Runtime reliability** — global scheduler pacing and the global metered
   budget.
5. **Historical reliability** — recurring provider failure evidence already
   derived from memory.

No new reliability database or cooldown store is introduced.

## Why the distinction matters

`RuntimeReliabilityStore` is global scheduler state. It should not be treated as
if every provider has failed.

Provider cooldown artifacts are narrower: they represent temporary provider
failures such as HTTP 429 or transient unavailability for a specific provider.

Historical reliability is different again: it describes recurring evidence from
past memory and is a degradation signal, not an automatic hard block.

The health snapshot preserves these distinctions.

## Health states

A provider may be reported as:

- `ready`
- `policy_blocked`
- `provider_cooldown`
- `metered_budget_blocked`
- `degraded_history`

Precedence is conservative:

1. policy block
2. active provider cooldown
3. global metered-budget block for a metered provider
4. recurring historical unreliability
5. ready

Historical degradation does not automatically disable a provider.

## Security and resource behavior

This module:

- performs no provider network requests;
- spends no money;
- does not reveal credential values;
- does not mutate cooldown files;
- does not mutate runtime reliability state;
- does not change autonomous routing;
- ignores malformed, expired, unknown, symlinked, and oversized cooldown files.

It reads the existing local state only.

## Diagnostic

```bash
python src/sira/provider_health.py --root ~/sira
```

To inspect the health picture when metered providers are policy-eligible:

```bash
python src/sira/provider_health.py \
  --root ~/sira \
  --allow-metered
```

`--allow-metered` does not authorize spending. Existing runtime metered-budget
state can still report the provider as blocked, and future spending authority
remains a separate gate.

## Future use

The structured snapshot is intended as input for later:

- Capability Broker
- Strategy Learning
- provider routing
- local Control Center
- diagnostics

Those integrations are intentionally not part of this step.
