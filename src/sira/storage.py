"""JSON persistence. This is local history, NOT tamper-proof storage."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from .models import utc_now


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def code_digest() -> str:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


class Cache:
    def __init__(self, root: Path, ttl_seconds: int):
        self.root, self.ttl_seconds = root, ttl_seconds

    def _path(self, key: str) -> Path:
        return self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def get(self, key: str):
        try:
            stored = json.loads(self._path(key).read_text(encoding="utf-8"))
            age = time.time() - stored["stored_at"]
            if self.ttl_seconds == 0 or not 0 <= age < self.ttl_seconds:
                return None
            return stored["value"]
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            return None

    def put(self, key: str, value) -> None:
        write_json(self._path(key), {"stored_at": time.time(), "value": value})


class RunStore:
    def __init__(self, root: Path):
        self.run_id = uuid4().hex
        self.path = root / "runs" / self.run_id
        self.path.mkdir(parents=True, mode=0o700)

    def event(self, event: str, **details) -> None:
        record = {"timestamp": utc_now(), "run_id": self.run_id, "event": event, **details}
        with (self.path / "audit.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n")
