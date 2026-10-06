"""Owner-controlled registry of public documentation hosts SIRA may read for learning.

Trust here means "worth reading as a source", never "true": every claim still needs
independent corroboration before it can become verified knowledge. External pages are
untrusted data, never instructions. The registry only widens which public HTTPS hosts a
learning goal may fetch; credentials, redirects, ports and private addresses stay
rejected by the existing fetch rules. The file lives under ``memory/`` (outside
candidate-editable code) and a malformed file grants nothing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from uuid import uuid4

SCHEMA = "sira.trusted_hosts.v1"
MAX_HOSTS = 200
_HOST_RE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")

# A starting point the owner may adopt with ``seed``. Never applied automatically.
SEED_HOSTS: tuple[str, ...] = (
    "docs.python.org", "peps.python.org", "packaging.python.org", "docs.pytest.org",
    "www.sqlite.org", "developer.mozilla.org", "www.w3.org", "datatracker.ietf.org",
    "www.rfc-editor.org", "owasp.org", "git-scm.com", "www.postgresql.org",
    "numpy.org", "pandas.pydata.org", "docs.github.com", "docs.docker.com",
    "kubernetes.io", "www.kernel.org",
)


def normalize_host(value: str) -> str:
    """Lowercase DNS name, no scheme/port/path/wildcard/IP. Raises ValueError otherwise."""
    if not isinstance(value, str):
        raise ValueError("host must be text")
    host = value.strip().lower().rstrip(".")
    if (not _HOST_RE.fullmatch(host) or len(host) > 253 or host.startswith("-")
            or not re.search(r"[a-z]", host.rsplit(".", 1)[-1])
            or host.endswith((".local", ".localhost", ".internal", ".invalid", ".test"))):
        raise ValueError(f"not a public hostname: {value!r}")
    return host


def _path(root: Path) -> Path:
    return Path(root) / "memory" / "trusted_hosts" / "registry.json"


def load_registry(root: Path) -> dict:
    """``{"valid": bool, "hosts": [...]}``. Absent file means empty and valid."""
    path = _path(root)
    if not path.exists() and not path.is_symlink():
        return {"valid": True, "hosts": [], "source": "default"}
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024:
            raise ValueError("unsafe registry")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("schema") != SCHEMA
                or set(data) != {"schema", "hosts"} or not isinstance(data["hosts"], list)
                or len(data["hosts"]) > MAX_HOSTS):
            raise ValueError("invalid registry")
        hosts = sorted({normalize_host(h) for h in data["hosts"]})
    except (OSError, ValueError, UnicodeError, TypeError):
        return {"valid": False, "hosts": [], "source": "malformed"}
    return {"valid": True, "hosts": hosts, "source": "owner_registry"}


def load_trusted_hosts(root: Path) -> frozenset[str]:
    """Hosts for the learning engine. A malformed registry yields no extra hosts."""
    return frozenset(load_registry(root)["hosts"])


def write_registry(root: Path, hosts) -> dict:
    cleaned = sorted({normalize_host(h) for h in hosts})
    if len(cleaned) > MAX_HOSTS:
        raise ValueError(f"at most {MAX_HOSTS} hosts")
    directory = _path(root).parent
    if directory.is_symlink() or Path(root, "memory").is_symlink():
        raise ValueError("unsafe registry directory")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".registry.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps({"schema": SCHEMA, "hosts": cleaned}), encoding="utf-8")
    os.replace(temporary, _path(root))
    return load_registry(root)


def add_hosts(root: Path, hosts) -> dict:
    current = load_registry(root)
    if not current["valid"]:
        raise ValueError("registry is malformed; fix or delete it first")
    return write_registry(root, [*current["hosts"], *hosts])


def remove_hosts(root: Path, hosts) -> dict:
    current = load_registry(root)
    if not current["valid"]:
        raise ValueError("registry is malformed; fix or delete it first")
    drop = {normalize_host(h) for h in hosts}
    return write_registry(root, [h for h in current["hosts"] if h not in drop])
