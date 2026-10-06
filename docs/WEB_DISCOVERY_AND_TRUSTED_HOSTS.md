# Budgeted web discovery + trusted documentation hosts

Evidence: learning goals searched only Wikimedia, DOAJ and arXiv although SIRA holds a Tavily
key; sources outside a 5-host list (plus .gov/.edu) could never be read, so most subjects had
no readable second source.

Parts
- `src/sira/trusted_hosts.py`: owner registry `memory/trusted_hosts/registry.json` of public
  documentation hosts SIRA may read. "Trusted" means worth reading, not true; claims still need
  independent corroboration. Never applied automatically (`seed` is an explicit owner action).
  The learning engine gains class `owner_trusted` (ranked after official/scholarly sources).
- `src/sira/web_search_discovery.py`: Tavily basic search (1 credit) restricted with
  `include_domains` to trusted + base hosts, merged into the goal's open-access results as
  discovery leads. Cache-first (7 days), results re-filtered to allowed hosts.

Budget (default OFF): `monthly_credits` (default 1000 = Tavily free plan) minus `owner_reserve`
(default 100) is the autonomous budget; a daily allowance spreads what remains over the rest
of the month; credits are reserved before the call; provider errors pause discovery for an hour
(or Retry-After). Manual `research` commands are not counted locally; the reserve covers them.
Check the real balance at app.tavily.com.

Control: `python tools/sira_web_discovery.py status|enable|disable|hosts list|seed|add|remove`.
Limits: wildcard domains are not supported; the starter host list is my selection and should be
reviewed; Tavily's `include_domains` behaviour is documented but was not exercised against the
live API in tests (an injected searcher is used).
