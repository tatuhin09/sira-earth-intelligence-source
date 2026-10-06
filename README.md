# SIRA — Stage 1 (v0.8)

**নতুন: free multi-provider scholarly-paper discovery + arXiv enrichment।** `python sira.py papers
"research question"` default `auto` mode-এ Semantic Scholar Academic Graph চেষ্টা করে,
প্রয়োজনে free public Crossref REST API-তে fallback করে, তারপর missing abstract/open-access
PDF metadata বা short result set থাকলে একটি bounded arXiv Atom query দিয়ে enrich/recover করে।
সর্বোচ্চ ৩টি ranked paper-এর title, abstract, authors, year/date, DOI, provider provenance ও
open-access PDF URL metadata সংরক্ষণ হয়; PDF download, paid provider বা model call হয় না।
২৪ ঘণ্টার local cache ও offline `benchmark --papers` আছে। বিস্তারিত [docs/PAPERS.md](docs/PAPERS.md)-এ। Evidence
synthesis flow আগের মতো [docs/SYNTHESIS.md](docs/SYNTHESIS.md)-এ আছে।

প্রথম কাজ: lightweight Python project, offline-tested controller এবং একটি
Tavily search provider। সব source code এই package-এ আছে; আলাদা snippet বসাতে হবে না।
শুধু Python standard library লাগে। কোনো local AI model, GPU, Docker বা paid
OpenAI API প্রয়োজন নেই। `pip install`-ও এই ধাপে লাগবে না।

বর্তমান output: search results, provenance-backed full text, exact passages,
locally gated claims, citations, reports, metrics ও audit log। Same-model দ্বিতীয়
pass independent final evaluator নয়; factual accuracy এখনও measured নয়।

## 1. Ubuntu tools

Ubuntu terminal-এ চালান:

```bash
sudo apt update && sudo apt install -y python3 python3-venv git unzip
python3 --version
git --version
```

Python 3.10 বা পরের version চাই। এই starter Python 3.12-তে পরীক্ষা করা হয়েছে।
Version দুটো দেখালে next step। PC-তে আগে থাকা tools থাকলে apt সেগুলোই রাখবে।

## 2. Project folder ও Python environment

ডাউনলোড করা ZIP-এর নাম রাখুন `sira-stage1-starter.zip`, location `~/Downloads/`।
নিচের command নতুন `~/sira/` তৈরি করবে। আগে folder থাকলে overwrite করবে না;
সেই ক্ষেত্রে থামুন এবং folder-এর বর্তমান অবস্থা জানান।

```bash
test ! -e "$HOME/sira" && unzip "$HOME/Downloads/sira-stage1-starter.zip" -d "$HOME"
```

Extract সফল হলে:

```bash
cd "$HOME/sira" && python3 -m venv .venv && source .venv/bin/activate
python -c "import sys; print(sys.executable)"
```

Expected: `/home/<আপনার-username>/sira/.venv/bin/python`।
পরবর্তী নতুন terminal-এ শুধু `cd ~/sira && source .venv/bin/activate`।
VS Code চাইলে এই folder থেকে `code .`।

## 3. Skeleton tests — key ছাড়াই

```bash
cd "$HOME/sira" && source .venv/bin/activate
python -m unittest discover -s tests -v
python sira.py benchmark
```

Expected: tests-এর শেষে `OK`; benchmark output-এ `"status": "passed"`,
`"passed": 3`, `"failed": 0`, `"api_requests": 0`।
Fail হলে পরের ধাপে যাবেন না। শেষ error lines পাঠাবেন; কোনো `.env` content নয়।

Offline controller হাতে পরীক্ষা:

```bash
python sira.py research "offline duplicate example" --provider fixture
```

Expected: `"status": "completed"`, `"sources": 2`, `"api_requests": 0`।
Output-এর `result` path খুললে source excerpts, citations ও metrics পাবেন।
একই command আবার দিলে `"cache_hit": true` দেখাবে।

## 4. প্রথম Git snapshot

সব test pass হলে:

```bash
git init -b main && git add . && git -c user.name="SIRA Local" -c user.email="sira@localhost" commit -m "feat: add tested SIRA Stage 1 retrieval starter"
git status --short
git log -1 --oneline
git check-ignore .env .env.local .venv/example .cache/example runs/example
```

Expected: `git status --short` ফাঁকা; log-এ একটি commit; check-ignore-এ পাঁচটি path।
`SIRA Local` শুধু এই commit-এর author label; GitHub account setup লাগবে না।
Git local history। এটি protected snapshot বা remote backup নয়।

## 5. প্রথম free search provider চালানো

এই অংশ শুধু আগের verification pass হওয়ার পরে।

1. [Tavily dashboard](https://app.tavily.com/) খুলে account তৈরি/sign in করুন।
2. Free plan-এ থাকুন এবং Development API key নিন। Paid plan/PAYGO চালু করবেন না।
3. Terminal-এ নিচের command চালিয়ে hidden prompt-এ key paste করুন:

```bash
python sira.py setup-key
```

Expected: `"status": "key_saved"`। Key terminal output-এ দেখা যাবে না।
এটি `~/sira/.env` তৈরি করে, permission `600`; Git ফাইলটি ignore করে।
আগে `.env` থাকলে command থামবে; সেটি local editor-এ সম্পাদনা করুন।
Supported format: শুধু `TAVILY_API_KEY=আপনার-আসল-key`, quote ছাড়া।
Environment-এর `TAVILY_API_KEY` থাকলে সেটি file-এর ওপর priority পায়।
`.env` কখনও `source` করতে হবে না; parser শুধু literal value পড়ে।

প্রথম live request:

```bash
python sira.py research "Python official documentation virtual environments"
```

Expected, service/key কাজ করলে: `"status": "completed"`, ১–৩টি source,
`"api_requests": 1`, সাধারণত `"reported_credits": 1`।
ফল না থাকলে `no_results`; error হলে code ও run path পাবেন।
একই command আবার চালালে cache hit এবং `api_requests: 0` হবে।
Fresh search ইচ্ছাকৃতভাবে করতে `--no-cache` দিন; এতে আবার API credit লাগতে পারে।

আপনার key না থাকায় এই package তৈরির সময় live request করা হয়নি। HTTP contract ও
error handling simulated responses দিয়ে পরীক্ষা করা হয়েছে। Live success দাবি নেই।

## Scholarly paper search — anonymous default, optional API key

Semantic Scholar-এর public Academic Graph search endpoint ব্যবহার করতে:

```bash
python sira.py papers "retrieval augmented generation factuality"
```

সাধারণ successful uncached run-এ একটি HTTP request হয় এবং সর্বোচ্চ তিনটি paper থাকে।
শুধু HTTP 429 এ SIRA সর্বোচ্চ ৩টি total attempt করে; `Retry-After` ৫ সেকেন্ডের
বেশি হলে long sleep না করে `rate_limited` result দেয়। একই query ২৪ ঘণ্টার মধ্যে
আবার চালালে local cache ব্যবহার হয়। Fresh metadata চাইলে:

```bash
python sira.py papers "retrieval augmented generation factuality" --no-cache
```

Output-এর `result` path-এর `papers.json`-এ rich metadata ও audit-linked run ID থাকে।
`open_access_pdf_url` থাকলেও SIRA এই command-এ PDF fetch/download করে না। Anonymous
endpoint throttled হলে short bounded retry হয়; limit থাকলে final code `rate_limited`।

Optional API key থাকলে hidden prompt দিয়ে locally save করতে:

```bash
python sira.py setup-paper-key
```

Key না থাকলেও command anonymous mode-এ চলে। Key কখনও output/audit log-এ লেখা হয় না।

Offline regression:

```bash
python sira.py benchmark --papers
```

Expected: `passed: 4`, `failed: 0`, `api_requests: 0`।


## Paper evidence → verified synthesis (v0.7)

After a successful paper search, convert its abstracts/open-access paper text into the same
provenance-checked evidence contract used by normal web research:

```bash
python sira.py paper-read <paper_run_id>
```

Abstracts are preferred and require zero network calls. A missing abstract may fall back to one
bounded open-access PDF fetch. Raw PDFs are not saved; only bounded extracted UTF-8 text is stored.
One PDF failure does not discard other readable papers. The evidence run can then use the existing
verified synthesis command directly:

```bash
python sira.py answer <paper_evidence_run_id>
```

Offline regression:

```bash
python sira.py benchmark --paper-reading
```

This step does not add a paid provider or a second answer engine. Paper text remains untrusted until
the existing synthesis verification gate accepts claims supported by its exact evidence passages.

## কোন file কোথায়

সব path `~/sira/`-এর ভেতরে; প্রতিটি file-এ সম্পূর্ণ code দেওয়া আছে।

| Path | কাজ |
|---|---|
| `sira.py` | Install ছাড়াই CLI চালু |
| `src/sira/config.py` | Settings ও literal key loading |
| `src/sira/models.py` | Source/SearchBatch/SearchProvider contract |
| `src/sira/controller.py` | একবারের research retrieval flow |
| `src/sira/providers/tavily.py` | প্রথম live web provider |
| `src/sira/providers/fixture.py` | Offline replay |
| `src/sira/papers.py` | Provider-neutral paper metadata, cache, dedupe ও run artifact |
| `src/sira/providers/semantic_scholar.py` | Semantic Scholar provider, optional API key ও bounded 429 handling |
| `src/sira/providers/crossref.py` | Free public Crossref scholarly metadata fallback |
| `src/sira/providers/arxiv.py` | Free arXiv Atom metadata + open-access PDF metadata |
| `src/sira/providers/paper_router.py` | Sequential failover, enrichment, merge/rank and diagnostics |
| `src/sira/paper_benchmark.py` | Offline scholarly-paper regression runner |
| `src/sira/paper_reading.py` | Paper metadata → provenance-checked evidence run |
| `src/sira/providers/pdf_text.py` | Bounded dependency-free open-access PDF text fallback |
| `src/sira/paper_reading_benchmark.py` | Offline paper-evidence regression runner |
| `src/sira/storage.py` | JSON cache, run files ও audit events |
| `src/sira/benchmark.py` | Offline regression runner |
| `src/sira/cli.py` | Commands, key setup, readable errors |
| `tests/test_sira.py` | Behavioural tests |
| `benchmarks/cases.json` | Fixed synthetic cases ও expected results |
| `benchmarks/paper_cases.json` | Offline paper metadata/dedupe/no-result cases |
| `benchmarks/paper_reading_cases.json` | Offline abstract/paper-evidence cases |
| `benchmarks/README.md` | Metrics ও evaluation সীমা |
| `docs/PROVIDERS.md` | যাচাই করা official API rules |
| `docs/VERIFICATION.md` | Package তৈরির সময় পরীক্ষার ফল |
| `runs/<run_id>/result.json` | Source, citation ও metrics |
| `runs/<run_id>/papers.json` | Scholarly paper metadata ও paper-run metrics |
| `runs/<run_id>/evidence.json` | Web or scholarly-paper provenance-checked evidence |
| `runs/<run_id>/audit.jsonl` | প্রতি line-এ structured event |
| `.cache/` | ২৪ ঘণ্টার local cache, Git-এর বাইরে |

Source, citation ও paper metadata JSON record হিসেবে রাখা হয়। GitHub provider বা
planner class প্রয়োজন হলে সেই ধাপে যোগ হবে।
`__init__.py` files package identity রাখে।

## Error diagnosis

| Error/output | করণীয় |
|---|---|
| `No such file ... sira-stage1-starter.zip` | `ls -lh ~/Downloads/sira-stage1-starter.zip`; নাম/location মিলান |
| Extract command-এ কিছু হচ্ছে না | `ls -ld ~/sira`; existing project overwrite করবেন না |
| `ensurepip is not available` | `sudo apt install python3-venv`, তারপর venv command আবার |
| `python: command not found` | `cd ~/sira && source .venv/bin/activate` |
| Missing `TAVILY_API_KEY` | `python sira.py setup-key` |
| `http_401` | Dashboard থেকে key যাচাই; ভুল env override থাকলে `unset TAVILY_API_KEY` |
| `rate_limited` | Forced Semantic Scholar mode was throttled |
| `all_paper_providers_failed` | Default paper-provider chain exhausted without a usable response |
| `http_432` / `http_433` | Account quota/usage limit দেখুন; paid upgrade নয় |
| `network_or_timeout` | Internet/DNS দেখুন; একটু পরে নিজে retry |
| `invalid_response` / `response_too_large` | Run ID ও error code পাঠান |
| `Storage error` | `df -h .` ও `ls -ld ~/sira ~/sira/runs` দিয়ে space/permission দেখুন |
| `no_results` | Query আরও নির্দিষ্ট করুন; retrieval success false থাকা স্বাভাবিক |

Common environment check:

```bash
cd "$HOME/sira" && source .venv/bin/activate
python --version
python -m unittest discover -s tests -v
git status --short
```

Ctrl+C foreground command cancel করে। এটি ভবিষ্যতের external STOP/PAUSE নয়।
Network read-এ ২০ second socket timeout এবং 2 MiB cap আছে; socket timeout মোট
wall-clock deadline নয়। Source webpage এখানে সরাসরি fetch করা হয় না।
Raw excerpts JSON data হিসেবে থাকে—code, command বা system instruction হিসেবে নয়।

## পরের কাজের নিয়ম

Semantic Scholar provider এখন bounded Stage 1 capability হিসেবে integrated। GitHub
বা অন্য provider যোগ করার আগে তাদের current official API rules আবার যাচাই করতে হবে।
Paper discovery নিজে factual verification নয়; paper-এর abstract/metadata downstream
reasoning-এর আগে evidence হিসেবে আলাদাভাবে যাচাই করতে হবে।

Provider implementation বদলালেও `SearchProvider.search(query, max_results)` ও
`SearchBatch` contract অক্ষুণ্ণ রাখলে controller পুনর্লিখতে হবে না। প্রতিটি
meaningful পরিবর্তনে একই regression cases এবং পরের live/human-reviewed benchmark
চালাতে হবে। উন্নতি হবে কি না তা ফল দিয়ে বিচার হবে; দ্রুত self-improvement-এর
কোনো guarantee এই কাঠামো দেয় না।

## Persistent learning memory (v0.8)

SIRA can now convert prior run outcomes into a local, searchable SQLite learning history without calling the network or a model:

```bash
python sira.py learn <RUN_ID>
python sira.py learn --all
python sira.py memory search "rate limit"
python sira.py memory show <MEMORY_ID>
python sira.py memory stats
python sira.py benchmark --memory
```

Repeated equivalent failures are fingerprint-deduplicated while every occurrence keeps its originating run/artifact SHA-256. Successful outcomes are retained as validated memories; rejected claims remain observed lessons. SQLite uses WAL mode plus a validated latest backup and quarantines a corrupt primary database before recovery. See `docs/MEMORY.md`.

## Isolated improvement loop (v0.9)

SIRA can now turn unresolved failure/rejection memory into a bounded, auditable improvement experiment without editing the main source tree:

```bash
python sira.py improve plan
python sira.py improve research <MEMORY_ID>
python sira.py improve experiment <HYPOTHESIS_ID>
python sira.py improve status
python sira.py benchmark --improvement
```

`improve plan` ranks unresolved memories by bounded frequency/category/kind weights. `improve research` searches related local memory, creates or reuses a deterministic hypothesis, and moves the lesson to `researched`. `improve experiment` creates an isolated candidate workspace, runs fixed offline unit/benchmark commands, and records a validated/rejected/blocked outcome. The experiment runner does not copy secrets, does not accept arbitrary shell commands, and **never promotes candidate code in v0.9**.

The offline improvement benchmark uses zero API requests. Candidate code editing, metric-aware before/after optimization, promotion and rollback are intentionally deferred to later milestones.

Rejected experiment hypotheses are fingerprinted and blocked from unchanged repetition; SIRA must research a new strategy rather than looping the same failed idea.

As of v0.9.2, `improve research` applies the same hygiene policy to its related-memory context: synthetic fixture/mock/dummy memories are excluded from autonomous root-cause context and validated-success counts. The research-context policy version is part of the hypothesis fingerprint, so a pre-v0.9.2 stale hypothesis is not silently reused after the policy changes.

## Autonomous runtime status (v1.0A-1)

The first autonomous-runtime stage is intentionally status-only. It persists no state just by inspecting it and fails closed to OFF if state is missing or malformed.

```bash
python sira.py self status
```

`self on` and `self off` arrive in the next staged patches after this state contract is verified.

## v1.0C-1 research-backed autonomous code writer

SIRA can now build a bounded project context from a researched improvement hypothesis and ask the configured Gemini model for a structured code patch. The writer sends only selected public project files; `.env`, runtime state, memory databases, run artifacts, symlinks, and protected-shell source are excluded from model context. Generated edits are complete UTF-8 file replacements and are applied only through the existing `ValidatedCandidateEditor` into an isolated candidate workspace.

This stage deliberately stops before Evaluator 2 / Evaluator 1 / promotion integration. `prepare_code_candidate(...)` records a local `code_writer_attempt.json`, reports model/API usage, and leaves the main public tree unchanged. The persistent `self on` worker will be connected to this writer in the next staged update.



## v1.0C-2 autonomous writer runtime integration

When the persistent `self on` worker researches a hypothesis without literal candidate edits, it now invokes the configured research-backed code writer and routes the generated patch through the existing isolated candidate, Evaluator 2, Evaluator 1, and transactional promotion/rollback chain. Writer/provider/configuration failures do not mutate the main tree; the cycle records the writer failure and falls back to the trusted non-mutating regression experiment. Generated-writer audit metadata is persisted with the autonomous promotion attempt.

## v1.0C-2c smart context budget

The autonomous code writer now starts with a focused context budget instead of repeatedly sending a broad project slice to metered model APIs. The first pass is capped at six complete files and 48 KiB of file content, while a ranked metadata-only context index lets the model identify other relevant files without receiving their contents. Explicit new-file tasks send no unrelated source files.

If the writer genuinely needs more source, it may request one bounded expansion of up to four exact non-protected paths. SIRA then sends those complete files plus focused evidence within a 160 KiB ceiling and allows one final patch attempt. Existing files are never accepted as edit targets unless their complete current contents were supplied in the active pass. Local computation and free/public research remain independent of this metered-model context budget.

## Local opportunity discovery (v1.0C-3a)

When no unresolved failure is available, SIRA can now inspect its modifiable Python source for deterministic local improvement opportunities without spending API quota:

```bash
python sira.py improve opportunities
```

The scanner excludes the protected shell, makes zero API requests, records evidence and priority for each opportunity, fingerprints source-specific findings, and suppresses recently attempted identical fingerprints for a bounded cooldown. Merely viewing a scan does not start cooldown; a later autonomous stage will call `OpportunityStore.mark_attempt()` when it actually works on an opportunity.

This stage is intentionally local-only. Future v1.0C-3 stages will add free/public research enrichment and then connect eligible opportunities to the existing autonomous writer -> Evaluator 2 -> Evaluator 1 -> transactional promotion/rollback pipeline. Metered model/API usage remains focused; local and genuinely free research can be used broadly while still respecting external provider rate limits.

## Opportunity evidence enrichment (v1.0C-3b)

A static opportunity is no longer treated as sufficient reason to rewrite code. Build a local evidence brief for a discovered opportunity first:

```bash
python sira.py improve opportunities --limit 5
python sira.py improve evidence <OPPORTUNITY_ID>
```

The evidence brief verifies the opportunity is still current, extracts the exact target function/module slice, finds local callers and test references, searches eligible learning memory when available, maps the target to a deterministic regression benchmark, and defines measurable success criteria. It makes zero network/model requests and does not start the opportunity cooldown by itself.

`assessment.decision: research_ready` requires an explicit benchmark mapping plus the behavioral oracle required by that signal type. Existing-code refactors require current test references; a `missing_test_reference` opportunity instead requires a real local caller, because requiring a test that does not yet exist would make that signal impossible to act on. Weak evidence remains `skip_weak_evidence`.


## Free/public opportunity research (v1.0C-3c)

Research-ready local evidence can now be enriched with bounded public scholarly metadata before any code writer handoff:

```bash
python sira.py improve free-research <EVIDENCE_ID>
```

The default provider set uses arXiv and Crossref only; no Gemini/model call is made and `paid_spending` stays `false`. Results are deduplicated by DOI/title, carry source URLs and provider provenance, and are cached for 24 hours so repeated research does not re-query public services unnecessarily. Provider failures and `Retry-After` values are recorded instead of triggering request floods.

`writer_handoff_allowed` becomes true only when the external brief contains at least two distinct sources and at least one abstract excerpt. Stale local evidence is rejected before network access. This stage still performs no code modification; v1.0C-3d will connect a sufficiently supported research brief to the existing isolated writer/evaluator/promotion pipeline.


## v1.0D-1 unified autonomous target scheduler

SIRA now has a local-only unified target selector that orders real benchmark/code-test regressions first, other eligible unresolved failure/rejection memories second, and proactive code opportunities third. The selector makes zero network/model calls and does not change the persistent `self on` worker in this stage.

Opportunity identity now has two layers: the existing source-sensitive fingerprint still controls exact-attempt cooldown, while a stable type/path/symbol lineage survives source changes. A successful promotion suppresses the same lineage for 24 hours and then applies a bounded 30-day diminishing-return priority penalty. Existing v1.0C-3d handoff artifacts are imported into lineage history, so an already-promoted function cannot immediately re-enter just because its source hash changed. Runtime integration of this scheduler is intentionally deferred to the next v1.0D stage.

## v1.0D-2 unified autonomous worker integration

`self on` now runs the v1.0D unified target selector on every persistent cycle. Eligible failure/rejection memory still uses the existing verified failure-improvement path, but the scheduler pins the exact selected memory so a proactive code-quality opportunity cannot displace a higher-priority regression. When no eligible memory target exists, a proactive opportunity flows through local evidence enrichment, free/public research, relevance gating, the focused Gemini code writer, structural-goal verification, Evaluator 2, Evaluator 1, and transactional promotion/rollback.

The worker checks runtime ownership and `self off` state between opportunity stages. If `self off` is requested while public research is in flight, the worker does not start a new writer/promotion transaction after the research call returns. Weak local evidence or insufficient external research records an opportunity attempt/cooldown without invoking the writer. Runtime status now reports stage `1.0D-2` and the last-cycle summary includes target-selection, opportunity, evidence, research, and handoff IDs for auditability.

## v1.0D-3/4 autonomous reliability and recovery

The persistent worker now applies bounded metered-model cadence, rolling request budgets, transient-provider backoff, configuration-failure pauses, interruptible idle waits, and persisted active-cycle recovery. An interrupted cycle is archived and re-selected rather than blindly resumed across a stale source state. `self off` prevents a new autonomous cycle from starting and wakes reliability waits promptly. The local `self reliability-check` diagnostic validates the budget, backoff, and interrupted-cycle policy without network/model calls.

## v1.1 multi-signal opportunity intelligence

Local opportunity discovery now uses a detector registry instead of treating branch complexity as the only function-level quality signal. The first v1.1 batch detects four deterministic signals: complex functions, public functions with no direct test reference, silently swallowed broad exception handlers, and exact duplicate function bodies within a module. All detection remains local-only and protected-shell files remain excluded.

Each new signal has an explicit measurable goal before writer handoff: missing-test opportunities must add a direct test call; weak broad-exception handling must reduce the verified weak-handler count; duplicate-body opportunities must reduce exact duplicate body matches; complex functions keep the branch-point reduction gate. The trusted promotion gate independently recomputes these metrics in main and candidate trees, so modifiable discovery code cannot self-assert that a goal was met. Research queries, relevance filters, and candidate strategies are signal-specific, while the existing benchmark, Evaluator 2, Evaluator 1, promotion, rollback, cooldown, and lineage controls remain in force.

## v1.2 bounded multi-worker foundation

SIRA now has an isolated coordination layer for future multi-worker autonomous improvement. Research and local-verification roles can run concurrently in separate task workspaces and persist separate artifacts; code work remains serialized and may be deferred by the existing global metered-resource budget. The coordinator cannot promote code. A single-owner promotion lease is provided only as a concurrency primitive for later integration with the existing Evaluator 2 -> Evaluator 1 -> transactional promotion/rollback authority. Interrupted worker tasks are abandoned and reselected rather than resumed from unknown partial state. `python sira.py self workers-check` validates the foundation offline without API/model calls.
