# Owner Notification Delivery

SIRA now has a durable local notification outbox for owner-action requests.

The first transport is Ubuntu desktop notification through `notify-send`.
Delivery is best-effort and never uses a shell. If the desktop session or
`notify-send` is unavailable, the autonomous runtime continues and the outbox
retries with bounded exponential backoff.

Access requests are synchronized into the outbox by request id, so one request
does not create repeated desktop notifications. Delivered events are historical
records and can be acknowledged separately from the underlying access request.

Approving an access request resolves the source notification requirement.
If its desktop notification has not yet been delivered, the stale outbox event
is cancelled instead of being shown later.

The outbox contains only sanitized metadata: resource, reason and requested
owner action. Credential values, passwords, tokens, card data and session
cookies are not stored.

Commands:

```bash
python src/sira/owner_notifications.py --root ~/sira list
python src/sira/owner_notifications.py --root ~/sira deliver
python src/sira/owner_notifications.py --root ~/sira ack nt_...
```

When the persistent autonomous loop is running, it pumps this outbox
automatically. Notification delivery failure is non-fatal to self-improvement.
