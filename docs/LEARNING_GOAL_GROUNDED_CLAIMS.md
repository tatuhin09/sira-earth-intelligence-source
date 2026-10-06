# Quote-grounded claim verification for learning goals

Why: independent websites almost never share one identical sentence, so the exact-sentence
rule verified 1 claim in 42 goal research reports. Source/document reading was not the
problem (0 near-identical sentence pairs among 9 multi-host artifacts).

How (module `src/sira/learning_goal_grounded_claims.py`, hooked in `learning_goal_runtime.py`):
1. After documents from >= 2 hosts are read and exact-sentence verification found nothing,
   verbatim topic-relevant sentences become numbered passages (E1..En).
2. The configured Gemini model PROPOSES atomic claims citing passage ids (it is never trusted).
3. A deterministic local gate accepts a claim only if: ids exist, no opposition, length and
   safety bounds, topic relevance, >= 2 distinct hosts each cover >= 50% of the claim terms,
   the union covers >= 80%, every number appears in two sources, polarity matches, and no
   other passage conflicts with the claim.
4. Accepted claims enter the existing evidence-gated knowledge store at confidence 0.80 with
   verifier_kind `grounded_claim_local_gate_v1` (exact-text verification stays at 0.85).
5. Provenance (passages, proposal, gate result, hashes) is saved under
   `memory/learning_goal_grounded/`. No API key is stored.

Limits: wording must reuse source words (no stemming), so many true claims are still
rejected; accepted claims are the lowest verified tier, not proof. Model requests are
bounded per UTC day, reserved before the call, and identical inputs are never re-sent.

Owner control (default: DISABLED): `python tools/sira_grounded_policy.py enable --daily 20`.
Disable with `disable`. Malformed policy fails closed. Measure the effect with
`sira_verification_diagnostics.py` (report outcomes before/after).
