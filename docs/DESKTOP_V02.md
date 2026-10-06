# SIRA Desktop v0.2 — Branding and UI Polish

v0.2 turns the v0.1 control-center foundation into a branded desktop product
without changing SIRA's autonomous authority model.

## Added

- custom vector SIRA logo and Ubuntu launcher icon;
- denser premium dashboard layout;
- dashboard rail with chat, quick actions and notifications;
- human-readable local date/time rendering;
- Research, Providers, Promotions and Tests & Health views;
- richer runtime/release/memory cards;
- provider policy visibility;
- protected promotion pipeline visibility;
- improved chat and activity presentation.

## Security preserved

- desktop server still binds only to `127.0.0.1`;
- state-changing POSTs still require the per-process session token;
- free-form chat still has no runtime start/stop authority;
- no new payment, package-install, promotion or external network authority;
- autonomous runtime remains owner-controlled.

## Chat scope

v0.2 chat is still local operational chat. Full model-backed conversation is
the next desktop stage and will be routed through SIRA's existing broker,
access and budget controls.
