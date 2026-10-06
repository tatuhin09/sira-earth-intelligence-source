# SIRA Stage 1, first slice

The user's supplied architecture is the approved design. This slice implements
one bounded search per invocation, with no local model, self-modification,
terminal execution, generated code execution, or repeat loop.

Use Python 3.10+ and only its standard library. The local process coordinates
Tavily REST search. Offline fixtures exercise the same controller. A protocol
separates providers from research orchestration; JSON artifacts are versioned.

Flow: CLI -> controller -> cache/provider -> validated source records -> numbered
evidence references -> result.json and audit.jsonl. Excerpts are untrusted data.
Source validation means field/URL validation and deduplication, not fact checking.
The report is a retrieval evidence packet, not a verified research answer.

Files: config.py (settings/key loading), models.py (contracts), providers/tavily.py
(HTTP), providers/fixture.py (offline replay), storage.py (cache/run persistence),
controller.py (single run), benchmark.py (fixture regression), cli.py (commands).
No empty paper/GitHub/planner modules. Future providers implement SearchProvider.

Boundaries: one request on cache miss, no retry or redirect, 20 second socket
timeout, 2 MiB response cap, 1-5 results, 500 character query cap. Cache lasts
24 hours. Explicit basic search, auto_parameters=false, include_answer=false,
include_raw_content=false, include_usage=true. Free plan is an account setting;
this client cannot guarantee no charge on a paid account. Never enable billing.

Benchmark foundation: code and dataset SHA-256, fixed fixture suite, recorded
latency, provider requests, reported credits, failures, retrieval presence,
deduplicated hostnames and reference resolution. Factual accuracy, semantic
citation correctness/coverage, source quality and genuine source diversity stay
null until independently evaluated. Hostname count is only a diversity proxy.
Fixture checks measure regression, not live quality or intelligence improvement.

Future protected evaluator, external STOP/PAUSE, broker, permissions and protected
snapshots must be deployed outside the future agent's writable process/account.
Separate folders and Git are NOT a security boundary or protected history.
Today Ctrl+C cancels the foreground run; it is not the future external STOP.

Local runs/cache may contain user questions and public excerpts and are ignored
by Git. Credentials and raw HTTP errors never enter artifacts. Runtime exceptions
are classified without copying arbitrary exception text. Keys live in environment
or optional literal .env, excluded from Git.

Acceptance: offline tests and regression pass without network/key; key setup
does not print secrets; mocked HTTP covers request shape, malformed data,
timeouts, limits and auth failures; live test is explicitly pending user key.
