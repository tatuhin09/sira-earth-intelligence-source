# SIRA Desktop v0.3 — Guarded Model-Backed Chat

v0.3 connects SIRA Desktop conversation to a bounded Gemini model path while
preserving owner control and the existing protected runtime architecture.

## Routing

1. Operational questions about status, memory, last cycle, notifications or
   Start/Stop remain local and use zero model requests.
2. General conversation enters the protected desktop-chat boundary.
3. Model use requires explicit owner confirmation that the configured Gemini
   project is intended for zero-cost/no-charge use.
4. The Capability Broker verifies the formal `desktop_chat` capability and
   credential availability.
5. A separate local desktop-chat monthly request cap limits interactive model
   use without reusing the autonomous loop's slow cadence.
6. The model receives bounded chat history, current runtime state and up to five
   relevant local memory summaries.
7. The Gemini request has no tools and returns structured JSON.
8. Every model attempt is audited locally.

## Authority

Desktop chat has no authority to:

- start or stop autonomous runtime from free-form text;
- promote code;
- edit protected code;
- install packages;
- authorize or perform payments;
- enable provider billing;
- read or expose secret values.

Start/Stop remains an explicit UI control backed by the existing protected
runtime controller.

## Cost protection

The default local cap is 100 Gemini desktop-chat requests per month. It can be
lowered or raised within 1..1000 using `SIRA_DESKTOP_CHAT_MONTHLY_CAP`.

The free-tier confirmation is a local SIRA safety gate. It does not change the
provider account or billing configuration and cannot guarantee how an external
provider bills a project. SIRA itself does not enable billing or authorize paid
spending.

## Research

The model can mark `needs_research=true` when fresh external evidence would
improve an answer. v0.3 does not silently browse the web from normal chat.
Explicit verified research routing can be added as a subsequent desktop stage.
