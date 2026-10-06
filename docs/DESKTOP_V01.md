# SIRA Desktop v0.1 — Local Control Center Foundation

This stage adds a localhost-only UI without changing SIRA's autonomous
engineering/runtime authority model.

## Security model

- HTTP binds only to `127.0.0.1`.
- No CORS headers are emitted.
- Every state-changing POST requires a random per-process session token in the
  custom `X-SIRA-Session` header.
- Free-form chat never starts or stops the runtime.
- Start/Stop use the existing protected `AutonomousRuntime`.
- Release refresh uses the existing protected v1.8L release gate.
- No new package, payment, promotion or network authority is added.

## UI areas

- Dashboard
- Chat
- Activity
- Memory
- Health
- Notifications
- Settings / local operation notes

## Chat v0.1

Chat is deliberately local and operational. It can explain current runtime
status, release health, memory counts, last cycle and notifications. It does
not yet invoke a model or external research provider. That is a later stage so
the model-backed conversation can be connected through the existing
capability/access/budget gates rather than bypassing them.

## Launch

Direct:

```bash
cd ~/sira
source .venv/bin/activate
PYTHONPATH=src python -m sira.desktop_app --root ~/sira --open
```

Per-user Ubuntu launcher:

```bash
python tools/install_sira_desktop.py --root ~/sira
```

The first launcher intentionally uses a standard system icon. A custom SIRA
logo/icon is the next visual-branding stage.
