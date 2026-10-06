# Provider Router Advisory

`src/sira/provider_router_advisory.py` is a read-only bridge between provider
health and a future routing layer. It does **not** replace or modify the current
paper/research routers.

It answers:

> Given a capability/domain and current provider health, which providers are
> sensible candidates right now, and in what deterministic advisory order?

## Ordering

1. ready free providers
2. historically degraded free providers
3. ready metered providers when existing policy allows them
4. historically degraded metered providers when existing policy allows them

Unavailable providers are not recommended:
- policy blocked
- required credential missing
- active provider cooldown
- metered budget blocked
- missing health state

Historical degradation lowers advisory priority but does not hard-block a
provider.

## Boundaries

This module:
- makes no provider network request;
- does not mutate current routers;
- writes no runtime state;
- records no metered attempt;
- does not authorize spending;
- does not expose credential values;
- does not implement Strategy Learning;
- does not implement the future Capability Broker.

## Usage

```bash
python src/sira/provider_router_advisory.py   --root ~/sira   --capability paper_search
```

```bash
python src/sira/provider_router_advisory.py   --root ~/sira   --capability repository_search
```

```bash
python src/sira/provider_router_advisory.py   --root ~/sira   --capability web_search   --allow-metered
```

`--allow-metered` is not spending authorization. Existing readiness, policy and
runtime-budget gates still apply.
