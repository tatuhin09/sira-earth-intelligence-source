# SIRA v0.5.1 — Compact Python symbol model context

v0.5.0 scoped the generated output but still passed the complete local target file to the model. v0.5.1 keeps the complete file only in local checksum-bound context and sends the exact target symbol block to Gemini. Scoped requests use a 16384 output-token ceiling; legacy/full-file requests keep 65536. Timeout, retries, evaluator authority, promotion authority, rollback, package-install and paid-spending authority are unchanged.
