# Owner Control CLI

SIRA's existing access-request and owner-notification stores are now exposed
through the main `sira.py` CLI.

This is a control surface only. It does not create new authority, does not store
secret values, and does not change payment or protected-shell policy.

Examples:

```bash
python sira.py access list
python sira.py access show ar_...
python sira.py access approve ar_...
python sira.py access satisfy ar_...
python sira.py access deny ar_...
python sira.py access cancel ar_...
python sira.py access resume-ready
python sira.py access consume-resume ar_...

python sira.py notifications list
python sira.py notifications deliver
python sira.py notifications ack nt_...
```

`approve` means the owner authorizes provisioning. It does not claim that the
credential or permission is actually available. `satisfy` is a separate state
transition after the required access has been provisioned.

Notification delivery remains best-effort. A delivery failure does not stop the
autonomous runtime.
