# Evidence verification ও answer synthesis — v0.3

এই version একটি existing evidence run থেকে bounded answer বানায়। এটি local
document hashes যাচাই করে, relevant passage বেছে নেয়, Gemini-কে সর্বোচ্চ দুইবার
call করে এবং deterministic local gate পাস করা claim-ই final report-এ লেখে।

## 1. Offline verification

Project root থেকে চালানোর command:

```bash
cd "$HOME/sira" && source .venv/bin/activate
python -m unittest discover -s tests -v &&
python sira.py benchmark &&
python sira.py benchmark --reading &&
python sira.py benchmark --synthesis
```

Expected output: tests শেষে `OK`; benchmark counts যথাক্রমে `3`, `4`, `5`;
সবগুলোতে `failed: 0` এবং `api_requests: 0`। JSON output-এর fieldগুলো command নয়।

## 2. Free-tier model key

[Google AI Studio](https://aistudio.google.com/apikey) থেকে Gemini API key নিন।
Google account/project-এ billing enable করবেন না। এই client account-এর plan
নিজে যাচাই বা billing বন্ধ করতে পারে না। Existing Tavily key overwrite না করে:

```bash
python sira.py setup-model-key
git check-ignore .env
```

Expected: hidden prompt-এর পরে `"name": "GEMINI_API_KEY"` ও `"status":
"key_saved"`; তারপর `.env` print হবে। Key terminal output বা chat-এ দেবেন না।

## 3. আপনার evidence থেকে answer

আপনার successful reading run ID দিয়ে exact command:

```bash
python sira.py answer 72f892c65ac84ef788dd30f0ec403050
```

সাধারণ syntax: `python sira.py answer EVIDENCE_RUN_ID`। এটি search run ID নয়;
`read` command যে নতুন ৩২-character `run_id` দিয়েছিল সেটি। First uncached run-এ
সর্বোচ্চ দুইটি model request হয়। একই command আবার চালালে expected
`api_requests: 0`, `cache_hits: 2`; নতুন audit run তবুও তৈরি হয়। প্রয়োজন ছাড়া
`--no-cache` দেবেন না, কারণ তাতে আবার দুইটি request হতে পারে।

Expected successful shape:

```json
{
  "status": "completed",
  "accepted_claims": 1,
  "metrics": {
    "api_requests": 2,
    "cache_hits": 0
  }
}
```

Claim support না পেলে `insufficient_evidence` স্বাভাবিক ও safe outcome; command
তবুও সফলভাবে শেষ হয়। Provider/format failure হলে `status: failed` এবং nonzero
exit হয়। Exact claim count model/evidence অনুযায়ী বদলাবে।

প্রতিটি answer run-এ পাবেন:

- `answer.json`: evidence hash, exact passage offsets/hashes, দুই pass-এর raw
  structured records, accepted/rejected claims, metrics এবং model/prompt version।
- `answer.md`: accepted claims ও source citations দিয়ে deterministic answer।
- `answer.html`: escaped static report; remote scripts/resources disabled।
- `audit.jsonl`: proposal, verification এবং final gate events।

## কীভাবে gate কাজ করে

Passage text untrusted JSON data। Model কোনো tool, search বা terminal পায় না।
Proposal-এর প্রতিটি atomic claim-এ passage ID লাগে। দ্বিতীয় call একই model দিয়ে
প্রতিটি claim আবার পরীক্ষা করে। Local code unknown/missing passage, missing
verdict, mixed/unsupported verdict, support disagreement বা opposition থাকলে
claim reject করে। Model নিজে final Markdown/HTML লিখতে পারে না।

`same_model_second_pass` useful consistency check, independent final evaluator
নয়। তাই `factual_accuracy`, objective `citation_correctness`, `source_quality`
এখনও `null`; structural citation metrics সত্যের score নয়। Future protected
evaluator এই mutable project-এর বাইরে থাকতে হবে।

## Files

| Path | দায়িত্ব |
|---|---|
| `src/sira/retrieval.py` | Evidence/document integrity এবং bounded passage selection |
| `src/sira/providers/gemini.py` | Gemini structured JSON transport; no tools/retry |
| `src/sira/synthesis.py` | Two-pass protocol, cache, local acceptance gate, run records |
| `src/sira/answer_reports.py` | Accepted claims থেকে deterministic Markdown/HTML |
| `src/sira/synthesis_benchmark.py` | Offline structural regression runner |
| `benchmarks/synthesis_cases.json` | Versioned supported/rejected/injection fixtures |
| `tests/test_synthesis.py` | Retrieval, gate, cache, provider ও report tests |

## Error diagnosis

| Output | করণীয় |
|---|---|
| `GEMINI_API_KEY missing` | `python sira.py setup-model-key` চালান |
| key already exists | `nano .env`; শুধু local value ঠিক করুন, content share করবেন না |
| evidence run missing/invalid | `ls runs/RUN_ID/evidence.json`; reading run ID মিলান |
| document integrity failed | Evidence run-এর document বদলেছে; original search থেকে নতুন `read` চালান |
| `http_401` | Google AI Studio key/project যাচাই করুন |
| `http_429` | Free-tier rate limit; `retry_after_seconds` মেনে পরে নিজে retry করুন |
| `network_or_timeout` | connection দেখুন; automatic retry নেই |
| `invalid_response` | Provider blocked/malformed/oversized output; run-এর audit দেখুন |
| `insufficient_evidence` | rejected reasons দেখুন; unsupported claim publish হয়নি |

Diagnosis commands:

```bash
python sira.py answer 72f892c65ac84ef788dd30f0ec403050
find runs -maxdepth 2 -name answer.json -printf '%T@ %p\n' | sort -nr | head
git status --short
```
