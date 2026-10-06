# SIRA v0.4.3 — Persistent Runtime Ownership Repair

The bounded cycle functions were updating shared runtime state before the
persistent scheduler could classify the cycle result. In persistent mode they
treated every failed cycle as terminal and wrote desired_state=off.

The repair keeps runtime ownership while keep_runtime_on=True unless an explicit
stop request or ownership/state change occurs. The outer scheduler remains the
authority that decides transient/configuration retry versus internal fail-closed
stop. Standalone one-cycle behavior remains fail-closed.

The rule is applied to both the memory cycle and unified opportunity cycle.
No new promotion, payment, package-installation, provider, or protected-shell
authority is added.
