# SIRA v1.7C-B — Semantic Teacher + Verifier Bridge

This stage adds a bounded multilingual semantic bridge on top of v1.7C-A.

## Flow

1. Local language profiling runs first.
2. English inputs that do not need semantic help remain local-only.
3. Bangla/Banglish/ambiguous inputs may use Gemini only when:
   - the caller explicitly enables the model bridge;
   - `SIRA_GEMINI_FREE_TIER_CONFIRMED=true`;
   - the local monthly language-model cap remains open;
   - Capability Broker reports Gemini ready.
4. One teacher call proposes a canonical English interpretation, research queries,
   and candidate token mappings.
5. A separate verifier call checks semantic equivalence, intent preservation, and
   every mapping.
6. Only verifier-supported mappings with confidence >= 0.80 become one evidence
   record.
7. v1.7C-A still requires at least two distinct verified evidence records before
   a mapping becomes active local knowledge.

A cache hit never creates new learning evidence.

## Missing access

If `GEMINI_API_KEY` is absent, the bridge creates the existing metadata-only owner
access request. The blocked language task is parked and the notification contains
the exact missing credential but never its value.

Missing zero-cost confirmation blocks execution and tells the owner exactly what
must be confirmed. It does not authorize billing.

## Cost boundary

The bridge has a local monthly request cap and uses two requests for an uncached
teacher+verifier pass. This local ledger cannot observe other applications using
the same Gemini project and cannot prove external provider billing state.

No payment, billing, subscription, quota purchase, authority, or promotion
permission is granted by this module.

## CLI

Local-only:

```bash
python sira.py language analyze "ami akhon code korbo"
```

Bounded semantic bridge:

```bash
SIRA_GEMINI_FREE_TIER_CONFIRMED=true \
python sira.py language bridge "ami akhon code korbo" --allow-model
```

Never set the confirmation flag unless the owner has verified that the intended
Gemini project is zero-cost/no-charge. SIRA must not enable billing to satisfy it.
