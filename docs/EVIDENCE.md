# Source reading ও evidence report — v0.2

এই ধাপে cloud-first reading যোগ হয়েছে। আগের search-এর source URLs Tavily Basic
Extract-এ পাঠানো হয়। Local browser/model/নতুন Python dependency লাগে না।

## আগে offline verification

নিচের block **চালানোর command**:

```bash
cd "$HOME/sira" && source .venv/bin/activate
python -m unittest discover -s tests -v &&
python sira.py benchmark &&
python sira.py benchmark --reading
```

Expected output — **এগুলো command নয়, terminal-এ paste করবেন না**:
tests শেষে OK; retrieval benchmark passed=3; reading benchmark passed=4;
দুই benchmark-এই failed=0 এবং api_requests=0।

## আগের run থেকে source পড়া

প্রথমে আপনার যাচাই করা search run ব্যবহার করুন। এই project-এর বর্তমান run ID দিয়ে
ঠিক command:

```bash
python sira.py read e218e20829aa490caea7bf10a1aa382b
```

সাধারণ syntax: `python sira.py read RUN_ID`। RUN_ID বলতে search-এর `result.json`
তৈরি করা ৩২ অক্ষরের ID; reading run-এর ID নয়। `--no-cache` দিলে document আবার
extract হবে, তাই প্রয়োজন ছাড়া দেবেন না।

প্রথম read-এ cache না থাকলে সর্বোচ্চ একটি Extract API request হবে, search request
হবে না। Basic extraction প্রতি পাঁচটি সফল URL-এ এক credit; API কখনও 0 reported
credit দেখাতে পারে কারণ counting aggregate হয়। Actual reported usage রাখা হবে।
এটি free-tier balance ব্যবহার করতে পারে। Account-এর paid/PAYGO চালু করবেন না।

Result status:

| Status | অর্থ |
|---|---|
| completed | সব source-এর text পড়া গেছে |
| partial | কিছু source পড়া গেছে; বাকিগুলোর status আলাদা আছে |
| failed | কোনো source পড়া যায়নি; source_statuses/error দেখুন |
| no_sources | parent search-এ source নেই |
| cancelled | Ctrl+C দিয়ে বন্ধ করা হয়েছে |

পূর্ণ success guarantee নেই; unavailable/blocked/paywalled/PDF/অস্বাভাবিক page
provider পড়তে নাও পারে। Reading ব্যর্থ হলে search excerpt-কে full text বলে
দেখানো হবে না। Failed documents cache হয় না; পরের explicit read-এ retry হবে।
Successful document cache ২৪ ঘণ্টা থাকবে, URL ও reader version অনুযায়ী।

## কোন files পাবেন

প্রতিটি read নতুন `~/sira/runs/<new_run_id>/` তৈরি করে:

- `evidence.json`: parent run ID/hash, document status, exact quotes, metrics।
- `documents/S1.txt`, `S2.txt` ইত্যাদি: provider থেকে পাওয়া text, পড়া গেলে।
- `report.md`: numbered source references-সহ readable report।
- `report.html`: browser-এ খোলার escaped static report।
- `audit.jsonl`: structured reading events ও final status।

CLI output-এর `report` value হলো report.html-এর পূর্ণ path। File manager থেকে
সেই file খুলুন, অথবা VS Code-এ project খুলে `runs`-এর সংশ্লিষ্ট folder দেখুন।
এখানে command ও output আলাদা; JSON field যেমন `"cache_hits": 3` command নয়।

`documents`-এ trust সবসময় untrusted থাকে। `provider_text_retrieved` অর্থ provider
লেখা দিয়েছে; এটি independent fact-check নয়। `quote_integrity=1.0` অর্থ quote
সংরক্ষিত text-এর start/end offset-এর সঙ্গে মেলে। Factual accuracy, semantic
citation correctness/coverage এবং source quality এখনও null/not evaluated।

Report-এর উদ্ধৃতি deterministic keyword overlap দিয়ে বাছাই হয়; এটি LLM-written
summary নয়। দীর্ঘ paragraph-এর শুরুর সর্বোচ্চ ৫০০ অক্ষর বিবেচনা করে, প্রতি source-এ
সর্বোচ্চ দুটি quote রাখে। Relevance বা completeness guarantee নেই। বাংলা প্রশ্নও
দেওয়া যায়; এই heuristic কোনো multilingual semantic model নয়।

## Knowledge base-এর জন্য ভিত্তি

Parent search ও reading run আলাদা এবং original search overwrite হয় না। প্রতিটি
document-এ canonical URL, provider, retrieval time, text path ও SHA-256 থাকে;
quote-এ source ID, character offsets ও content hash থাকে। Future RAG ingestion-এ
এই records ব্যবহার করা যাবে। Cloud database/vector index/memory promotion বা
model learning এখন যোগ হয়নি। Text retrieval training নয়।

Source text code, shell command, HTML script বা system instruction হিসেবে
execute হয় না। HTML report escaped এবং remote resources/scripts নিষিদ্ধ;
source link click করলে browser-এ external page খুলবে। Search ও extraction একই
provider হওয়ায় সেগুলোকে দুইটি independent verification বলা যাবে না।

## পরিবর্তিত ও নতুন code

সব path project `~/sira/` থেকে:

| Path | দায়িত্ব |
|---|---|
| src/sira/reading.py | Document/ExtractProvider contract, cache, reading run, provenance |
| src/sira/providers/tavily_extract.py | Basic cloud extraction, URL-to-document association |
| src/sira/providers/http_json.py | Search ও reading-এর shared bounded HTTP/error handling |
| src/sira/providers/tavily.py | একই search behaviour; shared transport ব্যবহার |
| src/sira/reports.py | Exact quote selection, escaped Markdown/HTML |
| src/sira/reading_benchmark.py | Offline reading regression |
| src/sira/cli.py | read ও benchmark --reading commands |
| tests/test_reading.py | Source reading, cache, failure, provenance ও injection tests |
| benchmarks/reading_cases.json | Fixed synthetic reading cases |

## সমস্যা হলে

- Missing run: `ls runs/e218e20829aa490caea7bf10a1aa382b/result.json` দিয়ে path দেখুন।
- Missing key: আগে থাকা `.env` এবং environment override দেখুন; key chat-এ দেবেন না।
- HTTP 401: key যাচাই। HTTP 429: returned retry_after_seconds মেনে পরে নিজে retry।
- HTTP 432/433: dashboard quota দেখুন; automatic paid fallback নেই।
- unsafe_url: obvious private/local বা unsupported URL; সেটি cloud extraction-এ যায়নি।
- missing_result/ambiguous_result: response-এ requested URL-এর নির্ভরযোগ্য matching নেই।
- extraction_failed/invalid_document: provider failure বা text validation failure।
- network_or_timeout/response_too_large: bounded request ব্যর্থ; status সংরক্ষিত।

এই version এখানে offline HTTP fixtures দিয়ে পরীক্ষা করা হয়েছে। আপনার local key
ব্যবহার করে প্রথম real extraction-এর output দেখেই live behaviour যাচাই করব।
