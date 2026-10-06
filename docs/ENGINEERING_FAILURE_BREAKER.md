# Engineering failure circuit breaker

Evidence (55 minute trial): 11 code-change attempts, 11 failures (7 rejected, 4 structural goal
not met), all of type `complex_function` ("reduce branch complexity"), 11 model requests,
0 promotions. An earlier run showed the same: one target retried 8 times, 8 rejections.

What: `src/sira/engineering_failure_breaker.py`, applied in the default
`_opportunity_discovery` of `autonomous_targeting.py`. When the owner enables it, an
opportunity type whose trailing run of failures reaches `failure_threshold` (default 5) is
hidden from discovery for 1h, 2h, 4h ... capped at `max_hold_hours` (default 24). A promoted
result resets the run; infrastructure outcomes neither count nor reset. Derived from saved
handoffs (last 7 days), so no state to repair. It only filters; it cannot approve or promote.

Control (default OFF): `python tools/sira_engineering_breaker.py status|enable|disable`.
Limit: it stops waste, it does not make attempts succeed. The loop still needs a better
strategy for code changes (smaller targets, failure memory in ranking); that is a separate step.
