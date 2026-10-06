# SIRA Competition Demo v1

Read-only, sanitized competition surface. The existing owner desktop remains unchanged.

Run locally:
```bash
cd ~/sira
source .venv/bin/activate
PYTHONPATH=src python -m sira.competition_demo --root ~/sira --port 8877
```
Open http://127.0.0.1:8877

Validate:
```bash
curl -fsS http://127.0.0.1:8877/healthz
curl -fsS http://127.0.0.1:8877/api/demo/overview | python -m json.tool
curl -i -X POST http://127.0.0.1:8877/api/demo/overview
```
POST must return 405 read_only_demo. Never tunnel owner desktop port 8765.
