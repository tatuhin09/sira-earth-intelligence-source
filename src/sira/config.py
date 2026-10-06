"""Explicit settings and literal credentials; nothing is evaluated as code."""
from dataclasses import dataclass
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class Settings:
    root: Path
    max_results: int = 3
    timeout_seconds: int = 20
    cache_ttl_seconds: int = 86400

    def __post_init__(self):
        if not 1 <= self.max_results <= 5:
            raise ValueError("max_results must be 1..5")
        if not 1 <= self.timeout_seconds <= 60 or self.cache_ttl_seconds < 0:
            raise ValueError("Invalid timeout or cache TTL")


SUPPORTED_CREDENTIALS = {
    "TAVILY_API_KEY",
    "GEMINI_API_KEY",
    "SIRA_SEMANTIC_SCHOLAR_API_KEY",
}


def validate_key(key: str, name="TAVILY_API_KEY") -> str:
    if name not in SUPPORTED_CREDENTIALS:
        raise ValueError("Unsupported credential name")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
        raise ValueError(f"{name} must be a literal key without spaces or shell syntax")
    return key


def load_optional_key(root: Path, name: str) -> str | None:
    """Load one supported literal credential without requiring that it exists."""
    if name not in SUPPORTED_CREDENTIALS:
        raise ValueError("Unsupported credential name")
    key = os.environ.get(name, "").strip()
    if not key and (root / ".env").is_file():
        for line in (root / ".env").read_text(encoding="utf-8").splitlines():
            variable, separator, value = line.partition("=")
            if separator and variable.strip() == name:
                key = value.strip()
                break
    return validate_key(key, name) if key else None


def load_key(root: Path, name="TAVILY_API_KEY") -> str:
    key = load_optional_key(root, name)
    if key is None:
        commands = {
            "TAVILY_API_KEY": "setup-key",
            "GEMINI_API_KEY": "setup-model-key",
            "SIRA_SEMANTIC_SCHOLAR_API_KEY": "setup-paper-key",
        }
        raise ValueError(f"{name} missing; run: python sira.py {commands[name]}")
    return key


def save_key(root: Path, name: str, key: str) -> Path:
    """Append one supported literal credential without replacing existing secrets."""
    if name not in SUPPORTED_CREDENTIALS:
        raise ValueError("Unsupported credential name")
    key = validate_key(key, name)
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".env"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(".env must be a regular file")
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    for line in existing.splitlines():
        variable, separator, _ = line.partition("=")
        if separator and variable.strip() == name:
            raise ValueError(f"{name} already exists; edit it locally without sharing its contents")
    separator = "" if not existing or existing.endswith("\n") else "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(f"{separator}{name}={key}\n")
    os.chmod(path, 0o600)
    return path
