# Provider Selection Policy

`src/sira/provider_policy.py` adds a deterministic, local-only provider selection
layer on top of:

- `provider_catalog.py`
- `provider_readiness.py`

This is **policy groundwork**, not runtime integration.

It does not make network requests and does not modify the existing autonomous
research router.

## Default behavior

The planner is intentionally conservative:

1. only catalogued providers are considered;
2. provider readiness is checked;
3. providers missing required credentials are blocked;
4. metered providers are blocked by default;
5. free providers are ordered before metered providers;
6. optional credentials may improve limits but are not required for providers
   that already work anonymously;
7. secret values are never serialized.

This matches SIRA's free/local-first resource policy.

## Examples

Paper research:

```bash
python src/sira/provider_policy.py --capability paper_search
```

Public GitHub repository research:

```bash
python src/sira/provider_policy.py --capability repository_search
```

Web search with the default policy:

```bash
python src/sira/provider_policy.py --capability web_search
```

If Tavily is the only matching provider, the default result remains blocked
because Tavily is marked `metered`.

An explicit diagnostic selection may enable metered providers:

```bash
python src/sira/provider_policy.py \
  --capability web_search \
  --allow-metered
```

This flag only makes the provider policy-eligible. It does **not** authorize
spending outside the caller's separate budget/spending gate.

## Important boundary

This module is not the future Capability Broker and should not become one by
accident.

It does not:

- load or reveal secret values;
- create accounts;
- authorize payments;
- mutate provider configuration;
- execute remote code;
- install packages;
- route autonomous runtime traffic yet.

Future Capability Broker / Strategy Learning can consume the structured plan
rather than rebuilding provider eligibility rules.
