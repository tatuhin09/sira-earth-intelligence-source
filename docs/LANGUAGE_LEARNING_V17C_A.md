# SIRA v1.7C-A — Multilingual Learning Foundation

This stage adds local multilingual profiling, Banglish detection, Unicode-safe
memory retrieval, evidence-gated learned language mappings, and reusable skill
consolidation.

Original text is always preserved. A normalized copy is derived separately.

A raw Gemini/web answer is **not** permanent learning. A language mapping needs
at least two distinct verified evidence items, average confidence >= 0.80, and no
conflicting verified meaning. A reusable skill needs one consistent procedure,
at least three distinct successful applications, confidence >= 0.75, no failed
application evidence, and no conflicting procedure.

Consolidated language/skill state never grants owner authority, OS permission,
payment authority, access authority, promotion authority, or protected-shell
authority.

Memory relevance tokenization now uses Unicode word tokens instead of ASCII-only
`[A-Za-z0-9]`, so Bengali and other scripts can participate in deterministic
memory retrieval.

CLI:

```bash
python sira.py language analyze "ami akhon code korbo"
python sira.py learning lexicon
python sira.py learning skills
python sira.py learning stats
```

v1.7C-B will add the bounded semantic-teacher + independent verification bridge.
Only verified outputs will be eligible to feed the consolidation store.
