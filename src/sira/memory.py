"""Local structured learning memory built from immutable run artifacts.

Run JSON stays the canonical provenance. SQLite stores searchable derived lessons.
No network, model, subprocess, or shell execution occurs in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from typing import Iterable
from uuid import uuid4

from .models import utc_now


SCHEMA_VERSION = 4
MACHINE_MEMORY_ORIGINS = {
    "run_artifact", "promotion_outcome", "rejection_outcome",
    "research_outcome", "autonomous_decision", "synthetic_fixture",
}
SYNTHETIC_MARKERS = ("fixture", "mock", "dummy", "synthetic")
MAX_ARTIFACT_BYTES = 12 * 1024 * 1024
MAX_SUMMARY_CHARS = 1200
MAX_DETAILS_CHARS = 12_000
MAX_SEARCH_CHARS = 300
MAX_SEARCH_RESULTS = 50
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
MEMORY_ID_RE = re.compile(r"m_[0-9a-f]{32}\Z")
RELATION_ID_RE = re.compile(r"mr_[0-9a-f]{32}\Z")
RELATION_TYPES = {"similar", "contradicts"}
MEMORY_OUTCOME_TYPES = {
    "retrieval_used", "retrieval_helped", "retrieval_irrelevant",
    "application_succeeded", "application_failed",
}
STATUSES = {"observed", "researched", "experimenting", "validated", "rejected", "superseded"}
CATEGORIES = {
    "network", "rate_limit", "bad_source", "provider", "parsing", "verification",
    "citation", "model", "benchmark_regression", "code_test", "unknown", "success",
}
ARTIFACT_ORDER = (
    "answer.json", "evidence.json", "papers.json", "result.json",
    "benchmark.json", "reading-benchmark.json", "synthesis-benchmark.json",
    "paper-reading-benchmark.json", "memory-benchmark.json", "improvement-benchmark.json",
)


class MemoryStoreError(OSError):
    pass


@dataclass(frozen=True)
class Observation:
    kind: str
    category: str
    capability: str
    summary: str
    status: str
    provider: str | None = None
    error_code: str | None = None
    signature: str | None = None
    details: dict | None = None
    origin: str = "run_artifact"
    synthetic: bool = False

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError("Invalid memory status")
        if self.category not in CATEGORIES:
            raise ValueError("Invalid memory category")
        if not self.kind or not self.capability or not self.summary.strip():
            raise ValueError("Observation fields must be nonempty")
        if self.origin not in MACHINE_MEMORY_ORIGINS:
            raise ValueError("Machine memory cannot store protected owner/user preference authority")
        if type(self.synthetic) is not bool:
            raise ValueError("Observation synthetic flag must be boolean")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(normalized.replace("_", " ").split())


def _bounded_text(value, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]



def _clamp01(value: float) -> float:
    return round(max(0.0, min(1.0, float(value))), 4)


def _synthetic_provider(provider: str | None) -> bool:
    normalized = _normalize_text(provider or "")
    return any(marker in normalized for marker in SYNTHETIC_MARKERS)


def memory_confidence_score(*, status: str, occurrence_count: int,
                            real_occurrence_count: int | None = None,
                            contradiction_count: int = 0,
                            source_diversity: int = 1,
                            category: str | None = None) -> float:
    """Deterministic evidence confidence. Freshness is intentionally excluded."""
    base = {
        "validated": 0.68,
        "researched": 0.52,
        "experimenting": 0.44,
        "observed": 0.30,
        "rejected": 0.15,
        "superseded": 0.05,
    }.get(status, 0.10)
    total = max(0, int(occurrence_count or 0))
    real = total if real_occurrence_count is None else max(0, int(real_occurrence_count or 0))
    effective = total if category == "benchmark_regression" else real
    recurrence_bonus = min(max(effective - 1, 0), 4) * 0.05
    diversity_bonus = min(max(int(source_diversity or 0) - 1, 0), 3) * 0.03
    contradiction_penalty = min(max(int(contradiction_count or 0), 0), 3) * 0.12
    synthetic_only_penalty = 0.25 if real == 0 and total > 0 and category != "benchmark_regression" else 0.0
    return _clamp01(base + recurrence_bonus + diversity_bonus
                    - contradiction_penalty - synthetic_only_penalty)


def memory_quality_score(*, status: str, occurrence_count: int,
                         real_occurrence_count: int | None = None,
                         contradiction_count: int = 0,
                         source_diversity: int = 1,
                         age_days: float = 0.0,
                         category: str | None = None) -> float:
    """Confidence plus a bounded freshness adjustment for retrieval/ranking."""
    confidence = memory_confidence_score(
        status=status, occurrence_count=occurrence_count,
        real_occurrence_count=real_occurrence_count,
        contradiction_count=contradiction_count,
        source_diversity=source_diversity, category=category,
    )
    age = max(0.0, float(age_days or 0.0))
    if age <= 7:
        freshness = 0.08
    elif age <= 30:
        freshness = 0.05
    elif age <= 90:
        freshness = 0.02
    elif age <= 180:
        freshness = 0.0
    elif age <= 365:
        freshness = -0.04
    else:
        freshness = -0.08
    return _clamp01(confidence + freshness)


def _observation_is_synthetic(observation: Observation) -> bool:
    return bool(observation.synthetic or observation.origin == "synthetic_fixture"
                or _synthetic_provider(observation.provider))


_CLUSTER_STOPWORDS = {
    "a", "an", "and", "as", "at", "by", "completed", "failure", "failed",
    "for", "from", "in", "is", "new", "old", "of", "on", "or", "result",
    "same", "success", "succeeded", "successfully", "the", "to", "via", "with",
}


def _unicode_context_words(value: str | None) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    result: list[str] = []
    current: list[str] = []

    def word_char(char: str) -> bool:
        category = unicodedata.category(char)
        return bool(category and category[0] in {"L", "N", "M"})

    for index, char in enumerate(normalized):
        if word_char(char):
            current.append(char)
            continue
        if (
            char in {"'", "’", "-"}
            and current
            and index + 1 < len(normalized)
            and word_char(normalized[index + 1])
        ):
            current.append(char)
            continue
        if current:
            result.append("".join(current))
            current = []
    if current:
        result.append("".join(current))
    return tuple(result)


def _relation_token(value: str) -> str:
    words = _unicode_context_words(str(value))
    token = "".join(words)
    if token.isascii():
        if len(token) > 4 and token.endswith("ies"):
            token = token[:-3] + "y"
        elif len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
    return token


def _context_tokens(value: str | None) -> set[str]:
    tokens: set[str] = set()
    for raw in _unicode_context_words(value):
        token = _relation_token(raw)
        if len(token) >= 2 and token not in _CLUSTER_STOPWORDS:
            tokens.add(token)
    return tokens


def _base_cluster_key(*, kind: str, category: str, capability: str,
                      provider: str | None) -> str:
    material = "|".join((
        _normalize_text(kind),
        _normalize_text(category),
        _normalize_text(capability),
        _normalize_text(provider or ""),
    ))
    return "cl_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _safe_relation_evidence(value: dict | None) -> str:
    if value is None:
        return "{}"
    if not isinstance(value, dict):
        raise ValueError("Relation evidence must be a dictionary")
    return _safe_details(value)


def _safe_details(value: dict | None) -> str:
    if not isinstance(value, dict):
        return "{}"
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded) > MAX_DETAILS_CHARS:
        encoded = json.dumps({"truncated": True, "sha256": _sha256_bytes(encoded.encode("utf-8"))})
    return encoded


def _category_for(code: str | None, *, rejection_reasons: Iterable[str] = ()) -> str:
    tokens = " ".join([code or "", *rejection_reasons]).lower()
    if "429" in tokens or "rate_limit" in tokens or "rate limit" in tokens:
        return "rate_limit"
    if any(x in tokens for x in ("timeout", "transport", "connection", "network", "dns", "tls")):
        return "network"
    if any(x in tokens for x in ("private_url", "unsafe", "blocked_url", "no_readable", "missing_result", "no_results")):
        return "bad_source"
    if any(x in tokens for x in ("citation", "unknown_passage", "unknown citation")):
        return "citation"
    if any(x in tokens for x in ("verdict", "unsupported", "mixed", "opposition", "verification")):
        return "verification"
    if any(x in tokens for x in ("json", "parse", "malformed", "invalid_pdf", "encrypted_pdf", "decode", "xml")):
        return "parsing"
    if any(x in tokens for x in ("model", "gemini", "blocked_response")):
        return "model"
    if any(x in tokens for x in ("benchmark", "regression")):
        return "benchmark_regression"
    if any(x in tokens for x in ("test", "compile", "code")):
        return "code_test"
    if any(x in tokens for x in ("http_", "provider", "api_", "server")):
        return "provider"
    return "unknown"


def _capability_for(artifact_name: str, data: dict) -> str:
    if artifact_name == "result.json":
        return "retrieval"
    if artifact_name == "papers.json":
        return "scholarly_research"
    if artifact_name == "evidence.json":
        return "paper_reading" if data.get("evidence_origin") == "scholarly_paper" else "reading"
    if artifact_name == "answer.json":
        return "synthesis"
    return "benchmarking"


def _provider_for(artifact_name: str, data: dict) -> str | None:
    if isinstance(data.get("provider"), str):
        return _bounded_text(data["provider"], 120)
    if artifact_name == "answer.json" and isinstance(data.get("model"), dict):
        return _bounded_text(data["model"].get("provider"), 120)
    if artifact_name == "evidence.json":
        return _bounded_text(data.get("reader"), 120)
    return _bounded_text(data.get("suite_id"), 120)


def _error_code(data: dict) -> str | None:
    error = data.get("error")
    return _bounded_text(error.get("code"), 160) if isinstance(error, dict) else None


def _success_summary(capability: str, provider: str | None) -> str:
    labels = {
        "retrieval": "retrieval completed successfully",
        "scholarly_research": "scholarly research completed successfully",
        "reading": "source reading completed successfully",
        "paper_reading": "paper reading completed successfully",
        "synthesis": "synthesis completed successfully",
        "benchmarking": "benchmark completed successfully",
    }
    base = labels.get(capability, f"{capability} completed successfully")
    return f"{base} via {provider}" if provider else base


def _failure_summary(capability: str, provider: str | None, code: str | None, status: str) -> str:
    label = (code or status or "unknown failure").replace("_", " ")
    prefix = f"{provider} " if provider else ""
    return f"{prefix}{label} failure in {capability}"[:MAX_SUMMARY_CHARS]


def _extract_observations(artifact_name: str, data: dict) -> list[Observation]:
    capability = _capability_for(artifact_name, data)
    provider = _provider_for(artifact_name, data)
    status = str(data.get("status", "")).strip().lower()
    code = _error_code(data)
    observations: list[Observation] = []

    if artifact_name.endswith("benchmark.json") or artifact_name in {
        "reading-benchmark.json", "synthesis-benchmark.json", "paper-reading-benchmark.json"
    }:
        failed = data.get("failed")
        if isinstance(failed, int) and failed == 0:
            observations.append(Observation(
                "success", "success", capability, _success_summary(capability, provider), "validated",
                provider=provider, signature=f"benchmark:{data.get('suite_id', artifact_name)}:passed",
                details={"passed": data.get("passed"), "failed": failed},
            ))
        elif isinstance(failed, int) and failed > 0:
            observations.append(Observation(
                "failure", "benchmark_regression", capability,
                f"benchmark regression: {failed} case(s) failed", "observed", provider=provider,
                error_code="benchmark_regression", signature=f"benchmark:{data.get('suite_id', artifact_name)}:failed",
                details={"passed": data.get("passed"), "failed": failed},
            ))
        return observations

    if status == "completed":
        observations.append(Observation(
            "success", "success", capability, _success_summary(capability, provider), "validated",
            provider=provider, signature=f"{capability}:{provider or 'none'}:completed",
            details={"status": status, "metrics": data.get("metrics", {})},
        ))
    elif status in {"failed", "cancelled", "no_sources", "no_results"} or code:
        effective_code = code or status or "unknown"
        observations.append(Observation(
            "failure", _category_for(effective_code), capability,
            _failure_summary(capability, provider, effective_code, status), "observed",
            provider=provider, error_code=effective_code,
            signature=f"{capability}:{provider or 'none'}:{effective_code}",
            details={"status": status, "error": data.get("error"), "metrics": data.get("metrics", {})},
        ))
    elif status == "partial":
        observations.append(Observation(
            "failure", "bad_source", capability, f"partial {capability} run with unavailable inputs",
            "observed", provider=provider, error_code="partial_run",
            signature=f"{capability}:{provider or 'none'}:partial",
            details={"status": status, "metrics": data.get("metrics", {})},
        ))

    if artifact_name == "answer.json":
        for row in data.get("rejected_claims", []) if isinstance(data.get("rejected_claims"), list) else []:
            if not isinstance(row, dict):
                continue
            text = _bounded_text(row.get("text"), 600) or "Rejected claim"
            reasons = [str(x)[:160] for x in row.get("reasons", []) if isinstance(x, str)]
            category = _category_for(None, rejection_reasons=reasons)
            signature = "|".join(sorted(_normalize_text(x) for x in reasons)) or "rejected_claim"
            observations.append(Observation(
                "rejection", category, "synthesis", f"Rejected claim: {text}"[:MAX_SUMMARY_CHARS],
                "observed", provider=provider, error_code=reasons[0] if reasons else "rejected_claim",
                signature=f"synthesis:rejection:{signature}:{_normalize_text(text)}",
                details={"claim_id": row.get("id"), "reasons": reasons},
            ))

    if artifact_name == "evidence.json":
        failed_statuses: dict[str, int] = {}
        for row in data.get("documents", []) if isinstance(data.get("documents"), list) else []:
            if isinstance(row, dict) and row.get("status") not in (None, "read"):
                key = str(row.get("status"))[:160]
                failed_statuses[key] = failed_statuses.get(key, 0) + 1
        for doc_status, count in sorted(failed_statuses.items()):
            observations.append(Observation(
                "failure", _category_for(doc_status), capability,
                f"{count} source(s) ended as {doc_status.replace('_', ' ')} in {capability}", "observed",
                provider=provider, error_code=doc_status,
                signature=f"{capability}:{provider or 'none'}:document:{doc_status}",
                details={"document_status": doc_status, "count": count},
            ))

    return observations


def _fingerprint(observation: Observation) -> str:
    payload = {
        "kind": observation.kind,
        "category": observation.category,
        "capability": observation.capability,
        "provider": _normalize_text(observation.provider or ""),
        "error_code": _normalize_text(observation.error_code or ""),
        "signature": _normalize_text(observation.signature or observation.summary),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _valid_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id) or "." in run_id:
        raise ValueError("Invalid run ID")
    return run_id


def _artifact_for_run(root: Path, run_id: str) -> tuple[Path, str, dict, str]:
    run_id = _valid_run_id(run_id)
    runs = (root / "runs").resolve()
    raw_run_dir = runs / run_id
    if raw_run_dir.is_symlink():
        raise ValueError("Run not found")
    run_dir = raw_run_dir.resolve()
    if run_dir.parent != runs or not run_dir.is_dir():
        raise ValueError("Run not found")
    for name in ARTIFACT_ORDER:
        path = run_dir / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("Run artifact is unsafe or too large")
        raw = path.read_bytes()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("Run artifact is invalid JSON") from None
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("Unsupported run artifact schema")
        embedded = data.get("run_id")
        if embedded is not None and embedded != run_id:
            raise ValueError("Run artifact ID mismatch")
        return path, name, data, _sha256_bytes(raw)
    raise ValueError("No supported run artifact found")


class MemoryStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.memory_dir = self.root / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.memory_dir / "sira_memory.sqlite3"
        self.backup_dir = self.memory_dir / "backups"
        self.backup_dir.mkdir(exist_ok=True, mode=0o700)
        self.backup_path = self.backup_dir / "sira_memory.latest.sqlite3"
        self.corrupt_dir = self.memory_dir / "corrupt"
        self._recover_if_needed()
        self._initialize()

    def _raw_connect(self, path: Path | None = None) -> sqlite3.Connection:
        conn = sqlite3.connect(path or self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _is_valid_database(path: Path) -> bool:
        if not path.is_file() or path.is_symlink():
            return False
        try:
            with closing(sqlite3.connect(path, timeout=2)) as conn:
                row = conn.execute("PRAGMA quick_check").fetchone()
                return bool(row and row[0] == "ok")
        except sqlite3.DatabaseError:
            return False

    def _recover_if_needed(self) -> None:
        if not self.db_path.exists():
            return
        if self.db_path.is_symlink() or not self.db_path.is_file():
            raise MemoryStoreError("Memory database path is not a regular file")
        if self._is_valid_database(self.db_path):
            return
        if not self._is_valid_database(self.backup_path):
            raise MemoryStoreError("Memory database is corrupt and no valid backup is available")
        self.corrupt_dir.mkdir(exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        quarantined = self.corrupt_dir / f"sira_memory.{stamp}.sqlite3"
        os.replace(self.db_path, quarantined)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.db_path) + suffix)
            if sidecar.exists() and sidecar.is_file() and not sidecar.is_symlink():
                sidecar.unlink()
        temporary = self.memory_dir / f".restore-{uuid4().hex}.sqlite3"
        try:
            shutil.copy2(self.backup_path, temporary)
            os.replace(temporary, self.db_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _connect(self) -> sqlite3.Connection:
        conn = self._raw_connect()
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        memory_columns = self._table_columns(conn, "memories")
        occurrence_columns = self._table_columns(conn, "occurrences")
        memory_additions = {
            "real_occurrence_count": "INTEGER NOT NULL DEFAULT 0 CHECK(real_occurrence_count >= 0)",
            "synthetic_occurrence_count": "INTEGER NOT NULL DEFAULT 0 CHECK(synthetic_occurrence_count >= 0)",
            "origin_kind": "TEXT NOT NULL DEFAULT 'run_artifact'",
            "confidence": "REAL NOT NULL DEFAULT 0.0 CHECK(confidence >= 0.0 AND confidence <= 1.0)",
            "contradiction_count": "INTEGER NOT NULL DEFAULT 0 CHECK(contradiction_count >= 0)",
            "source_diversity": "INTEGER NOT NULL DEFAULT 1 CHECK(source_diversity >= 0)",
            "supersedes_memory_id": "TEXT",
            "superseded_by_memory_id": "TEXT",
            "lineage_version": "INTEGER NOT NULL DEFAULT 1 CHECK(lineage_version >= 1)",
        }
        occurrence_additions = {
            "origin_kind": "TEXT NOT NULL DEFAULT 'run_artifact'",
            "is_synthetic": "INTEGER NOT NULL DEFAULT 0 CHECK(is_synthetic IN (0,1))",
        }
        for name, declaration in memory_additions.items():
            if name not in memory_columns:
                conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {declaration}")
        for name, declaration in occurrence_additions.items():
            if name not in occurrence_columns:
                conn.execute(f"ALTER TABLE occurrences ADD COLUMN {name} {declaration}")
        conn.execute(
            """UPDATE occurrences SET is_synthetic=1
               WHERE memory_id IN (
                   SELECT memory_id FROM memories
                   WHERE lower(coalesce(provider,'')) LIKE '%fixture%'
                      OR lower(coalesce(provider,'')) LIKE '%mock%'
                      OR lower(coalesce(provider,'')) LIKE '%dummy%'
                      OR lower(coalesce(provider,'')) LIKE '%synthetic%'
               )"""
        )
        for row in conn.execute("SELECT memory_id FROM memories").fetchall():
            self._refresh_memory_metrics(conn, row[0])
        conn.execute("UPDATE metadata SET value=? WHERE key='schema_version'", ("2",))

    def _migrate_v2_to_v3(self, conn: sqlite3.Connection) -> None:
        memory_columns = self._table_columns(conn, "memories")
        if "cluster_key" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN cluster_key TEXT")
        if "cluster_size" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN cluster_size INTEGER NOT NULL DEFAULT 1 CHECK(cluster_size >= 1)")
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS memory_relations ("
            "relation_id TEXT PRIMARY KEY,"
            "memory_a_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE RESTRICT,"
            "memory_b_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE RESTRICT,"
            "relation_type TEXT NOT NULL CHECK(relation_type IN ('similar','contradicts')),"
            "confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),"
            "reason TEXT NOT NULL,"
            "evidence_json TEXT NOT NULL DEFAULT '{}',"
            "created_at TEXT NOT NULL,"
            "CHECK(memory_a_id < memory_b_id),"
            "UNIQUE(memory_a_id,memory_b_id,relation_type)"
            ");"
            "CREATE INDEX IF NOT EXISTS idx_memory_relations_a ON memory_relations(memory_a_id);"
            "CREATE INDEX IF NOT EXISTS idx_memory_relations_b ON memory_relations(memory_b_id);"
            "CREATE INDEX IF NOT EXISTS idx_memory_relations_type ON memory_relations(relation_type);"
        )
        self._rebuild_clusters(conn)
        conn.execute("UPDATE metadata SET value=? WHERE key='schema_version'", ("3",))

    def _migrate_v3_to_v4(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_outcomes (
                outcome_id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE RESTRICT,
                outcome_type TEXT NOT NULL CHECK(outcome_type IN (
                    'retrieval_used','retrieval_helped','retrieval_irrelevant',
                    'application_succeeded','application_failed'
                )),
                weight REAL NOT NULL CHECK(weight >= 0.0 AND weight <= 4.0),
                context_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memory_outcomes_memory
                ON memory_outcomes(memory_id,created_at);
            CREATE INDEX IF NOT EXISTS idx_memory_outcomes_type
                ON memory_outcomes(outcome_type);
            """
        )
        conn.execute("UPDATE metadata SET value=? WHERE key='schema_version'", ("4",))

    def _initialize(self) -> None:
        try:
            with closing(self._connect()) as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS memories (
                        memory_id TEXT PRIMARY KEY,
                        fingerprint TEXT NOT NULL UNIQUE,
                        kind TEXT NOT NULL,
                        category TEXT NOT NULL,
                        capability TEXT NOT NULL,
                        provider TEXT,
                        error_code TEXT,
                        summary TEXT NOT NULL,
                        status TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        occurrence_count INTEGER NOT NULL CHECK(occurrence_count >= 0),
                        real_occurrence_count INTEGER NOT NULL DEFAULT 0 CHECK(real_occurrence_count >= 0),
                        synthetic_occurrence_count INTEGER NOT NULL DEFAULT 0 CHECK(synthetic_occurrence_count >= 0),
                        origin_kind TEXT NOT NULL DEFAULT 'run_artifact',
                        confidence REAL NOT NULL DEFAULT 0.0 CHECK(confidence >= 0.0 AND confidence <= 1.0),
                        contradiction_count INTEGER NOT NULL DEFAULT 0 CHECK(contradiction_count >= 0),
                        source_diversity INTEGER NOT NULL DEFAULT 1 CHECK(source_diversity >= 0),
                        supersedes_memory_id TEXT,
                        superseded_by_memory_id TEXT,
                        lineage_version INTEGER NOT NULL DEFAULT 1 CHECK(lineage_version >= 1),
                        cluster_key TEXT,
                        cluster_size INTEGER NOT NULL DEFAULT 1 CHECK(cluster_size >= 1)
                    );
                    CREATE TABLE IF NOT EXISTS occurrences (
                        occurrence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        memory_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE RESTRICT,
                        run_id TEXT NOT NULL,
                        artifact_name TEXT NOT NULL,
                        artifact_sha256 TEXT NOT NULL,
                        outcome_status TEXT,
                        observed_at TEXT NOT NULL,
                        details_json TEXT NOT NULL,
                        origin_kind TEXT NOT NULL DEFAULT 'run_artifact',
                        is_synthetic INTEGER NOT NULL DEFAULT 0 CHECK(is_synthetic IN (0,1)),
                        UNIQUE(memory_id, run_id, artifact_sha256)
                    );
                    CREATE TABLE IF NOT EXISTS transitions (
                        transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        memory_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE RESTRICT,
                        from_status TEXT NOT NULL,
                        to_status TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        run_id TEXT,
                        changed_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS memory_relations (
                        relation_id TEXT PRIMARY KEY,
                        memory_a_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE RESTRICT,
                        memory_b_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE RESTRICT,
                        relation_type TEXT NOT NULL CHECK(relation_type IN ('similar','contradicts')),
                        confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
                        reason TEXT NOT NULL,
                        evidence_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        CHECK(memory_a_id < memory_b_id),
                        UNIQUE(memory_a_id,memory_b_id,relation_type)
                    );
                    CREATE TABLE IF NOT EXISTS memory_outcomes (
                        outcome_id TEXT PRIMARY KEY,
                        memory_id TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE RESTRICT,
                        outcome_type TEXT NOT NULL CHECK(outcome_type IN (
                            'retrieval_used','retrieval_helped','retrieval_irrelevant',
                            'application_succeeded','application_failed'
                        )),
                        weight REAL NOT NULL CHECK(weight >= 0.0 AND weight <= 4.0),
                        context_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
                    CREATE INDEX IF NOT EXISTS idx_memories_capability ON memories(capability);
                    CREATE INDEX IF NOT EXISTS idx_memories_last_seen ON memories(last_seen_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_occurrences_run ON occurrences(run_id);
                    CREATE INDEX IF NOT EXISTS idx_memory_relations_a ON memory_relations(memory_a_id);
                    CREATE INDEX IF NOT EXISTS idx_memory_relations_b ON memory_relations(memory_b_id);
                    CREATE INDEX IF NOT EXISTS idx_memory_relations_type ON memory_relations(relation_type);
                    CREATE INDEX IF NOT EXISTS idx_memory_outcomes_memory ON memory_outcomes(memory_id,created_at);
                    CREATE INDEX IF NOT EXISTS idx_memory_outcomes_type ON memory_outcomes(outcome_type);
                    """
                )
                row = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
                migrated = False
                if row is None:
                    conn.execute("INSERT INTO metadata(key,value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
                    self._rebuild_clusters(conn)
                elif row[0] == "1":
                    self._migrate_v1_to_v2(conn)
                    self._migrate_v2_to_v3(conn)
                    self._migrate_v3_to_v4(conn)
                    migrated = True
                elif row[0] == "2":
                    self._migrate_v2_to_v3(conn)
                    self._migrate_v3_to_v4(conn)
                    migrated = True
                elif row[0] == "3":
                    self._migrate_v3_to_v4(conn)
                    migrated = True
                elif row[0] != str(SCHEMA_VERSION):
                    raise MemoryStoreError("Unsupported memory schema version")
                else:
                    self._rebuild_clusters(conn)
                conn.commit()
        except sqlite3.DatabaseError as exc:
            raise MemoryStoreError("Unable to initialize memory database") from exc
        if migrated or not self._is_valid_database(self.backup_path):
            self._backup()

    def _backup(self) -> None:
        fd, temporary_name = tempfile.mkstemp(prefix=".memory-backup-", suffix=".sqlite3", dir=self.backup_dir)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            with closing(self._connect()) as source, closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
                row = destination.execute("PRAGMA quick_check").fetchone()
                if not row or row[0] != "ok":
                    raise MemoryStoreError("Memory backup integrity check failed")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.backup_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _rebuild_clusters(conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT memory_id,kind,category,capability,provider FROM memories").fetchall()
        if not rows:
            return
        parent = {str(r["memory_id"]): str(r["memory_id"]) for r in rows}
        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        def union(l, r):
            root_l, root_r = find(l), find(r)
            if root_l != root_r:
                parent[root_r] = root_l
        base_for = {str(r["memory_id"]): _base_cluster_key(kind=str(r["kind"]), category=str(r["category"]), capability=str(r["capability"]), provider=r["provider"]) for r in rows}
        by_base = {}
        for mid, base in base_for.items():
            if base in by_base:
                union(by_base[base], mid)
            else:
                by_base[base] = mid
        for la, lb in conn.execute("SELECT memory_a_id,memory_b_id FROM memory_relations WHERE relation_type='similar'").fetchall():
            if str(la) in parent and str(lb) in parent:
                union(str(la), str(lb))
        components = {}
        for mid in parent:
            components.setdefault(find(mid), []).append(mid)
        for members in components.values():
            bases = sorted({base_for[mid] for mid in members})
            cluster_key = bases[0] if len(bases) == 1 else "cl_" + hashlib.sha256("|".join(bases).encode("utf-8")).hexdigest()[:32]
            conn.executemany("UPDATE memories SET cluster_key=?, cluster_size=? WHERE memory_id=?", [(cluster_key, len(members), mid) for mid in members])

    @staticmethod
    def _decode_relation(row: sqlite3.Row, memory_id: str) -> dict:
        value = dict(row)
        other = value["memory_b_id"] if value["memory_a_id"] == memory_id else value["memory_a_id"]
        try:
            evidence = json.loads(value.pop("evidence_json"))
            if not isinstance(evidence, dict):
                evidence = {"corrupt_evidence": True}
        except (ValueError, TypeError):
            evidence = {"corrupt_evidence": True}
            value.pop("evidence_json", None)
        value["other_memory_id"] = other
        value["evidence"] = evidence
        return value

    @staticmethod
    def _refresh_memory_metrics(conn: sqlite3.Connection, memory_id: str) -> None:
        row = conn.execute(
            """SELECT status,category,contradiction_count,source_diversity
               FROM memories WHERE memory_id=?""", (memory_id,),
        ).fetchone()
        if row is None:
            return
        counts = conn.execute(
            """SELECT COUNT(*),
                      COALESCE(SUM(CASE WHEN is_synthetic=0 THEN 1 ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN is_synthetic=1 THEN 1 ELSE 0 END),0)
               FROM occurrences WHERE memory_id=?""", (memory_id,),
        ).fetchone()
        total, real_count, synthetic_count = map(int, counts)
        confidence = memory_confidence_score(
            status=row["status"], occurrence_count=total, real_occurrence_count=real_count,
            contradiction_count=int(row["contradiction_count"] or 0),
            source_diversity=int(row["source_diversity"] or 0), category=row["category"],
        )
        conn.execute(
            """UPDATE memories SET occurrence_count=?, real_occurrence_count=?,
                      synthetic_occurrence_count=?, confidence=? WHERE memory_id=?""",
            (total, real_count, synthetic_count, confidence, memory_id),
        )

    def upsert(self, observation: Observation, *, run_id: str, artifact_name: str,
               artifact_sha256: str, outcome_status: str | None) -> tuple[str, bool]:
        run_id = _valid_run_id(run_id)
        fingerprint = _fingerprint(observation)
        now = utc_now()
        memory_id = "m_" + uuid4().hex
        details_json = _safe_details(observation.details)
        inserted_occurrence = False
        is_synthetic = int(_observation_is_synthetic(observation))
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT memory_id FROM memories WHERE fingerprint=?", (fingerprint,)).fetchone()
                if row is None:
                    conn.execute(
                        """INSERT INTO memories(
                               memory_id,fingerprint,kind,category,capability,provider,error_code,
                               summary,status,first_seen_at,last_seen_at,occurrence_count,
                               real_occurrence_count,synthetic_occurrence_count,origin_kind,confidence,
                               contradiction_count,source_diversity,lineage_version,cluster_key,cluster_size
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,0.0,0,1,1,NULL,1)""",
                        (memory_id, fingerprint, observation.kind, observation.category, observation.capability,
                         observation.provider, observation.error_code, observation.summary[:MAX_SUMMARY_CHARS],
                         observation.status, now, now, observation.origin),
                    )
                else:
                    memory_id = row[0]
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO occurrences(
                           memory_id,run_id,artifact_name,artifact_sha256,outcome_status,observed_at,
                           details_json,origin_kind,is_synthetic
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (memory_id, run_id, artifact_name, artifact_sha256, outcome_status, now,
                     details_json, observation.origin, is_synthetic),
                )
                inserted_occurrence = cursor.rowcount == 1
                if inserted_occurrence:
                    conn.execute("UPDATE memories SET last_seen_at=? WHERE memory_id=?", (now, memory_id))
                    self._refresh_memory_metrics(conn, memory_id)
                self._rebuild_clusters(conn)
                conn.commit()
        except sqlite3.DatabaseError as exc:
            raise MemoryStoreError("Memory write failed") from exc
        if inserted_occurrence:
            self._backup()
        return memory_id, inserted_occurrence

    def relate(
        self,
        memory_a_id: str,
        memory_b_id: str,
        relation_type: str,
        confidence: float,
        reason: str,
        *,
        evidence: dict | None = None,
    ) -> dict:
        if not MEMORY_ID_RE.fullmatch(memory_a_id) or not MEMORY_ID_RE.fullmatch(memory_b_id):
            raise ValueError("Invalid memory ID")
        if memory_a_id == memory_b_id:
            raise ValueError("A memory cannot relate to itself")
        if relation_type not in RELATION_TYPES:
            raise ValueError("Invalid memory relation type")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("Relation confidence must be numeric")
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("Relation confidence must be between 0 and 1")
        reason = _bounded_text(reason, 1000)
        if not reason:
            raise ValueError("Relation reason is required")
        evidence_json = _safe_relation_evidence(evidence)

        left, right = sorted((memory_a_id, memory_b_id))
        now = utc_now()
        created = False
        with closing(self._connect()) as conn:
            a = conn.execute("SELECT * FROM memories WHERE memory_id=?", (left,)).fetchone()
            b = conn.execute("SELECT * FROM memories WHERE memory_id=?", (right,)).fetchone()
            if a is None or b is None:
                raise ValueError("Memory not found")
            if relation_type == "contradicts":
                if int(a["real_occurrence_count"] or 0) <= 0 or int(b["real_occurrence_count"] or 0) <= 0:
                    raise ValueError("synthetic-only memory cannot penalize real memory confidence")

            existing = conn.execute(
                "SELECT * FROM memory_relations WHERE memory_a_id=? AND memory_b_id=? AND relation_type=?",
                (left, right, relation_type),
            ).fetchone()
            if existing is None:
                relation_id = "mr_" + uuid4().hex
                conn.execute(
                    "INSERT INTO memory_relations("
                    "relation_id,memory_a_id,memory_b_id,relation_type,confidence,reason,evidence_json,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (relation_id, left, right, relation_type, confidence, reason, evidence_json, now),
                )
                created = True
                if relation_type == "contradicts":
                    conn.execute(
                        "UPDATE memories SET contradiction_count=contradiction_count+1 WHERE memory_id IN (?,?)",
                        (left, right),
                    )
                    self._refresh_memory_metrics(conn, left)
                    self._refresh_memory_metrics(conn, right)
                if relation_type == "similar":
                    self._rebuild_clusters(conn)
                existing = conn.execute(
                    "SELECT * FROM memory_relations WHERE relation_id=?", (relation_id,)
                ).fetchone()
            conn.commit()

        if created:
            self._backup()
        return self._decode_relation(existing, memory_a_id)

    def relations(self, memory_id: str) -> list[dict]:
        if not MEMORY_ID_RE.fullmatch(memory_id):
            raise ValueError("Invalid memory ID")
        with closing(self._connect()) as conn:
            exists = conn.execute("SELECT 1 FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            if exists is None:
                raise ValueError("Memory not found")
            rows = conn.execute(
                "SELECT * FROM memory_relations WHERE memory_a_id=? OR memory_b_id=? ORDER BY created_at,relation_id",
                (memory_id, memory_id),
            ).fetchall()
            return [self._decode_relation(row, memory_id) for row in rows]

    def cluster(self, memory_id: str) -> dict:
        if not MEMORY_ID_RE.fullmatch(memory_id):
            raise ValueError("Invalid memory ID")
        with closing(self._connect()) as conn:
            self._rebuild_clusters(conn)
            row = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            if row is None:
                raise ValueError("Memory not found")
            cluster_key = row["cluster_key"]
            members = [
                self._memory_row(item)
                for item in conn.execute(
                    "SELECT * FROM memories WHERE cluster_key=? ORDER BY first_seen_at,memory_id",
                    (cluster_key,),
                ).fetchall()
            ]
            conn.commit()
        return {"cluster_key": cluster_key, "cluster_size": len(members), "members": members}

    @staticmethod
    def _opposing_outcomes(left: sqlite3.Row, right: sqlite3.Row) -> bool:
        failure_kinds = {"failure", "rejection"}
        return (
            (str(left["kind"]) == "success" and str(right["kind"]) in failure_kinds)
            or (str(right["kind"]) == "success" and str(left["kind"]) in failure_kinds)
        )

    @staticmethod
    def _contradiction_compatible(left: sqlite3.Row, right: sqlite3.Row) -> tuple[bool, int]:
        if int(left["real_occurrence_count"] or 0) <= 0 or int(right["real_occurrence_count"] or 0) <= 0:
            return False, 0
        if not MemoryStore._opposing_outcomes(left, right):
            return False, 0
        if _normalize_text(left["capability"]) != _normalize_text(right["capability"]):
            return False, 0
        left_provider = _normalize_text(left["provider"] or "")
        right_provider = _normalize_text(right["provider"] or "")
        if not left_provider or left_provider != right_provider:
            return False, 0
        overlap = _context_tokens(left["summary"]) & _context_tokens(right["summary"])
        provider_tokens = _context_tokens(left_provider)
        meaningful = overlap - provider_tokens
        return (len(meaningful) >= 1 or len(overlap) >= 3), len(overlap)

    def reconcile_candidate(self, memory_id: str) -> list[dict]:
        if not MEMORY_ID_RE.fullmatch(memory_id):
            raise ValueError("Invalid memory ID")
        with closing(self._connect()) as conn:
            candidate = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            if candidate is None:
                raise ValueError("Memory not found")
            others = conn.execute(
                "SELECT * FROM memories WHERE memory_id<>? ORDER BY last_seen_at DESC,memory_id",
                (memory_id,),
            ).fetchall()
            proposals: list[dict] = []
            candidate_base = _base_cluster_key(
                kind=str(candidate["kind"]), category=str(candidate["category"]),
                capability=str(candidate["capability"]), provider=candidate["provider"],
            )
            for other in others:
                other_id = str(other["memory_id"])
                compatible, overlap_count = self._contradiction_compatible(candidate, other)
                if compatible:
                    proposals.append({
                        "other_memory_id": other_id,
                        "relation_type": "contradicts",
                        "confidence": _clamp01(0.82 + min(overlap_count, 4) * 0.03),
                        "reason": "same provider/capability with materially opposing real outcomes",
                        "evidence": {"detector": "deterministic_reconciliation", "shared_context_tokens": overlap_count},
                    })
                    continue
                other_base = _base_cluster_key(
                    kind=str(other["kind"]), category=str(other["category"]),
                    capability=str(other["capability"]), provider=other["provider"],
                )
                if candidate_base == other_base:
                    proposals.append({
                        "other_memory_id": other_id,
                        "relation_type": "similar",
                        "confidence": 0.82,
                        "reason": "same bounded kind/category/capability/provider context",
                        "evidence": {"detector": "deterministic_context_cluster"},
                    })
        proposals.sort(key=lambda row: (0 if row["relation_type"] == "contradicts" else 1, -float(row["confidence"]), row["other_memory_id"]))
        return proposals

    def transition(self, memory_id: str, to_status: str, reason: str, run_id: str | None = None) -> None:
        if not MEMORY_ID_RE.fullmatch(memory_id):
            raise ValueError("Invalid memory ID")
        if to_status not in STATUSES:
            raise ValueError("Invalid target status")
        reason = _bounded_text(reason, 1000)
        if not reason:
            raise ValueError("Transition reason is required")
        if run_id is not None:
            _valid_run_id(run_id)
        allowed = {
            "observed": {"researched", "validated", "rejected", "superseded"},
            "researched": {"experimenting", "validated", "rejected", "superseded"},
            "experimenting": {"researched", "validated", "rejected", "superseded"},
            "validated": {"superseded"},
            "rejected": {"researched", "superseded"},
            "superseded": set(),
        }
        now = utc_now()
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT status FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            if row is None:
                raise ValueError("Memory not found")
            current = row[0]
            if to_status == current:
                return
            if to_status not in allowed[current]:
                raise ValueError(f"Invalid memory transition: {current} -> {to_status}")
            conn.execute(
                "INSERT INTO transitions(memory_id,from_status,to_status,reason,run_id,changed_at) VALUES(?,?,?,?,?,?)",
                (memory_id, current, to_status, reason, run_id, now),
            )
            conn.execute("UPDATE memories SET status=?, last_seen_at=? WHERE memory_id=?", (to_status, now, memory_id))
            self._refresh_memory_metrics(conn, memory_id)
            conn.commit()
        self._backup()

    def supersede(self, older_memory_id: str, newer_memory_id: str, reason: str,
                  run_id: str | None = None) -> None:
        if not MEMORY_ID_RE.fullmatch(older_memory_id) or not MEMORY_ID_RE.fullmatch(newer_memory_id):
            raise ValueError("Invalid memory ID")
        if older_memory_id == newer_memory_id:
            raise ValueError("A memory cannot supersede itself")
        reason = _bounded_text(reason, 1000)
        if not reason:
            raise ValueError("Supersession reason is required")
        if run_id is not None:
            _valid_run_id(run_id)
        now = utc_now()
        with closing(self._connect()) as conn:
            older = conn.execute("SELECT * FROM memories WHERE memory_id=?", (older_memory_id,)).fetchone()
            newer = conn.execute("SELECT * FROM memories WHERE memory_id=?", (newer_memory_id,)).fetchone()
            if older is None or newer is None:
                raise ValueError("Memory not found")
            if older["status"] == "superseded":
                if older["superseded_by_memory_id"] == newer_memory_id:
                    return
                raise ValueError("Memory is already superseded by another lesson")
            conn.execute(
                "INSERT INTO transitions(memory_id,from_status,to_status,reason,run_id,changed_at) VALUES(?,?,?,?,?,?)",
                (older_memory_id, older["status"], "superseded", reason, run_id, now),
            )
            conn.execute(
                """UPDATE memories SET status='superseded', superseded_by_memory_id=?, last_seen_at=?
                   WHERE memory_id=?""", (newer_memory_id, now, older_memory_id),
            )
            next_version = max(int(newer["lineage_version"] or 1), int(older["lineage_version"] or 1) + 1)
            conn.execute(
                "UPDATE memories SET supersedes_memory_id=?, lineage_version=? WHERE memory_id=?",
                (older_memory_id, next_version, newer_memory_id),
            )
            self._refresh_memory_metrics(conn, older_memory_id)
            self._refresh_memory_metrics(conn, newer_memory_id)
            conn.commit()
        self._backup()

    @staticmethod
    def _memory_row(row: sqlite3.Row) -> dict:
        value = dict(row)
        try:
            seen = datetime.fromisoformat(str(value["last_seen_at"]).replace("Z", "+00:00"))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (datetime.now(timezone.utc) - seen.astimezone(timezone.utc)).total_seconds() / 86400)
        except (TypeError, ValueError, OverflowError):
            age_days = 3650.0
        value["quality_score"] = memory_quality_score(
            status=value.get("status", "observed"),
            occurrence_count=int(value.get("occurrence_count") or 0),
            real_occurrence_count=int(value.get("real_occurrence_count") or 0),
            contradiction_count=int(value.get("contradiction_count") or 0),
            source_diversity=int(value.get("source_diversity") or 0),
            age_days=age_days, category=value.get("category"),
        )
        return value

    @staticmethod
    def _autonomous_candidate_eligible(row: dict) -> bool:
        # A failing benchmark is actionable even when its regression suite uses fixtures.
        if row.get("category") == "benchmark_regression":
            return True
        if "real_occurrence_count" in row:
            return int(row.get("real_occurrence_count") or 0) > 0
        return not _synthetic_provider(str(row.get("provider") or ""))


    @staticmethod
    def _decode_outcome(row: sqlite3.Row) -> dict:
        value = dict(row)
        try:
            context = json.loads(value.pop("context_json"))
            if not isinstance(context, dict):
                context = {"corrupt_context": True}
        except (ValueError, TypeError):
            context = {"corrupt_context": True}
            value.pop("context_json", None)
        value["context"] = context
        return value

    def record_outcome(
        self,
        memory_id: str,
        outcome_type: str,
        *,
        context: dict | None = None,
        weight: float = 1.0,
    ) -> dict:
        if not MEMORY_ID_RE.fullmatch(memory_id):
            raise ValueError("Invalid memory ID")
        if outcome_type not in MEMORY_OUTCOME_TYPES:
            raise ValueError("Invalid memory outcome type")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ValueError("Memory outcome weight must be numeric")
        weight = float(weight)
        if not 0.0 <= weight <= 4.0:
            raise ValueError("Memory outcome weight must be between 0 and 4")
        if context is not None and not isinstance(context, dict):
            raise ValueError("Memory outcome context must be a dictionary")

        context_json = _safe_details(context or {})
        outcome_id = "mo_" + uuid4().hex
        now = utc_now()
        with closing(self._connect()) as conn:
            exists = conn.execute(
                "SELECT 1 FROM memories WHERE memory_id=?", (memory_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("Memory not found")
            conn.execute(
                """INSERT INTO memory_outcomes(
                       outcome_id,memory_id,outcome_type,weight,context_json,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (outcome_id, memory_id, outcome_type, weight, context_json, now),
            )
            row = conn.execute(
                "SELECT * FROM memory_outcomes WHERE outcome_id=?", (outcome_id,)
            ).fetchone()
            conn.commit()
        self._backup()
        return self._decode_outcome(row)

    def outcomes(self, memory_id: str) -> list[dict]:
        if not MEMORY_ID_RE.fullmatch(memory_id):
            raise ValueError("Invalid memory ID")
        with closing(self._connect()) as conn:
            exists = conn.execute(
                "SELECT 1 FROM memories WHERE memory_id=?", (memory_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("Memory not found")
            rows = conn.execute(
                """SELECT * FROM memory_outcomes
                   WHERE memory_id=?
                   ORDER BY created_at,outcome_id""",
                (memory_id,),
            ).fetchall()
            return [self._decode_outcome(row) for row in rows]

    @staticmethod
    def _retrieval_age_days(last_seen_at: str | None) -> float:
        try:
            seen = datetime.fromisoformat(str(last_seen_at).replace("Z", "+00:00"))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            return max(
                0.0,
                (
                    datetime.now(timezone.utc) - seen.astimezone(timezone.utc)
                ).total_seconds() / 86400,
            )
        except (TypeError, ValueError, OverflowError):
            return 3650.0

    @staticmethod
    def _freshness_component(age_days: float) -> tuple[float, str]:
        age = max(0.0, float(age_days or 0.0))
        if age <= 7:
            return 0.08, "boost"
        if age <= 30:
            return 0.05, "boost"
        if age <= 90:
            return 0.02, "boost"
        if age <= 180:
            return 0.0, "neutral"
        if age <= 365:
            return -0.04, "penalty"
        return -0.08, "penalty"

    @staticmethod
    def _retrieval_feedback_weights(
        conn: sqlite3.Connection, memory_id: str,
    ) -> tuple[float, float, float]:
        """Aggregate outcome weights used by retrieval scoring and explanations."""
        outcome_rows = conn.execute(
            """SELECT outcome_type,weight FROM memory_outcomes
               WHERE memory_id=?""",
            (memory_id,),
        ).fetchall()
        helped = 0.0
        irrelevant = 0.0
        used = 0.0
        for outcome in outcome_rows:
            kind = str(outcome["outcome_type"])
            value = max(0.0, float(outcome["weight"] or 0.0))
            if kind in {"retrieval_helped", "application_succeeded"}:
                helped += value
            elif kind in {"retrieval_irrelevant", "application_failed"}:
                irrelevant += value
            elif kind == "retrieval_used":
                used += value
        return helped, irrelevant, used

    def retrieve(
        self,
        query: str,
        limit: int = 20,
        *,
        autonomous: bool = False,
    ) -> list[dict]:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= MAX_SEARCH_CHARS:
            raise ValueError("Memory retrieval query must be 1..300 characters")
        if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise ValueError("Memory retrieval limit must be 1..50")
        if type(autonomous) is not bool:
            raise ValueError("Memory autonomous retrieval flag must be boolean")

        query_tokens = _context_tokens(query)
        if not query_tokens:
            query_tokens = {
                token for token in _normalize_text(query).split()
                if token
            }

        results: list[dict] = []
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT * FROM memories ORDER BY last_seen_at DESC,memory_id"
                ).fetchall()
                for db_row in rows:
                    row = self._memory_row(db_row)
                    if autonomous and not self._autonomous_candidate_eligible(row):
                        continue

                    searchable = " ".join(
                        str(row.get(key) or "")
                        for key in (
                            "summary", "category", "capability",
                            "provider", "error_code",
                        )
                    )
                    haystack = _context_tokens(searchable)
                    overlap = query_tokens & haystack
                    if not overlap:
                        continue

                    relevance = min(
                        1.0,
                        len(overlap) / max(1, len(query_tokens)),
                    )
                    confidence = _clamp01(float(row.get("confidence") or 0.0))
                    age_days = self._retrieval_age_days(row.get("last_seen_at"))
                    freshness_value, freshness_effect = self._freshness_component(age_days)

                    if row.get("category") == "benchmark_regression":
                        recurrence_count = int(row.get("occurrence_count") or 0)
                    else:
                        recurrence_count = int(row.get("real_occurrence_count") or 0)
                    recurrence_value = min(max(recurrence_count - 1, 0), 4) * 0.0125

                    diversity = max(0, int(row.get("source_diversity") or 0))
                    diversity_value = min(max(diversity - 1, 0), 3) * 0.01

                    helped, irrelevant, used = self._retrieval_feedback_weights(
                        conn, row["memory_id"]
                    )

                    feedback_value = (
                        min(helped, 3.0) * 0.05
                        - min(irrelevant, 3.0) * 0.05
                    )
                    contradiction_count = max(
                        0, int(row.get("contradiction_count") or 0)
                    )
                    contradiction_value = -min(contradiction_count, 3) * 0.04

                    raw_score = (
                        relevance * 0.45
                        + confidence * 0.30
                        + freshness_value
                        + recurrence_value
                        + diversity_value
                        + feedback_value
                        + contradiction_value
                    )
                    score = _clamp01(raw_score)

                    reasons = [
                        {
                            "component": "relevance",
                            "effect": "boost",
                            "value": round(relevance * 0.45, 4),
                            "matched_terms": sorted(overlap),
                        },
                        {
                            "component": "confidence",
                            "effect": "boost" if confidence > 0 else "neutral",
                            "value": round(confidence * 0.30, 4),
                        },
                        {
                            "component": "freshness",
                            "effect": freshness_effect,
                            "value": round(freshness_value, 4),
                            "age_days": round(age_days, 2),
                        },
                    ]

                    if recurrence_value:
                        reasons.append({
                            "component": "recurrence",
                            "effect": "boost",
                            "value": round(recurrence_value, 4),
                            "real_occurrences": int(row.get("real_occurrence_count") or 0),
                        })
                    if diversity_value:
                        reasons.append({
                            "component": "source_diversity",
                            "effect": "boost",
                            "value": round(diversity_value, 4),
                            "source_diversity": diversity,
                        })
                    if feedback_value != 0.0 or used > 0.0:
                        reasons.append({
                            "component": "outcome_feedback",
                            "effect": (
                                "boost" if feedback_value > 0
                                else "penalty" if feedback_value < 0
                                else "neutral"
                            ),
                            "value": round(feedback_value, 4),
                            "retrieval_helped_weight": round(helped, 4),
                            "retrieval_irrelevant_weight": round(irrelevant, 4),
                            "retrieval_used_weight": round(used, 4),
                        })
                    if contradiction_count:
                        reasons.append({
                            "component": "contradiction",
                            "effect": "penalty",
                            "value": round(contradiction_value, 4),
                            "contradiction_count": contradiction_count,
                        })

                    row["retrieval_score"] = score
                    row["score_reasons"] = reasons
                    results.append(row)
        except sqlite3.DatabaseError as exc:
            raise MemoryStoreError("Memory retrieval failed") from exc

        results.sort(
            key=lambda row: (
                float(row["retrieval_score"]),
                float(row.get("quality_score") or 0.0),
                str(row.get("last_seen_at") or ""),
                str(row.get("memory_id") or ""),
            ),
            reverse=True,
        )
        return results[:limit]


    def improvement_candidates(self, limit: int = 5) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Improvement candidate limit must be 1..20")
        category_weight = {
            "benchmark_regression": 9, "code_test": 8, "verification": 7, "citation": 6,
            "model": 5, "provider": 4, "rate_limit": 4, "network": 4,
            "parsing": 3, "bad_source": 2, "unknown": 1,
        }
        kind_weight = {"failure": 3, "rejection": 2}
        try:
            with closing(self._connect()) as conn:
                rows = [dict(row) for row in conn.execute(
                    "SELECT * FROM memories WHERE kind IN ('failure','rejection') "
                    "AND status IN ('observed','rejected')"
                ).fetchall()]
        except sqlite3.DatabaseError as exc:
            raise MemoryStoreError("Improvement candidate query failed") from exc
        rows = [row for row in rows if self._autonomous_candidate_eligible(row)]
        for row in rows:
            recurrence = (int(row.get("occurrence_count") or 0)
                          if row.get("category") == "benchmark_regression"
                          else int(row.get("real_occurrence_count") or 0))
            row["priority_score"] = (min(recurrence, 20) * 10
                                     + category_weight.get(row.get("category"), 1)
                                     + kind_weight.get(row.get("kind"), 0))
        rows.sort(key=lambda row: (row["priority_score"], row.get("last_seen_at") or ""), reverse=True)
        return rows[:limit]

    def search(self, query: str, limit: int = 20) -> list[dict]:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= MAX_SEARCH_CHARS:
            raise ValueError("Memory search query must be 1..300 characters")
        if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise ValueError("Memory search limit must be 1..50")
        terms = [t for t in _normalize_text(query).split() if t]
        clauses, params = [], []
        for term in terms:
            clauses.append("lower(summary || ' ' || category || ' ' || capability || ' ' || coalesce(provider,'') || ' ' || coalesce(error_code,'')) LIKE ?")
            params.append(f"%{term}%")
        sql = "SELECT * FROM memories WHERE " + " AND ".join(clauses) + " ORDER BY last_seen_at DESC LIMIT ?"
        params.append(limit)
        try:
            with closing(self._connect()) as conn:
                return [self._memory_row(row) for row in conn.execute(sql, params).fetchall()]
        except sqlite3.DatabaseError as exc:
            raise MemoryStoreError("Memory search failed") from exc

    def show(self, memory_id: str) -> dict:
        if not MEMORY_ID_RE.fullmatch(memory_id):
            raise ValueError("Invalid memory ID")
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            if row is None:
                raise ValueError("Memory not found")
            result = self._memory_row(row)
            occurrences = []
            for item in conn.execute("SELECT * FROM occurrences WHERE memory_id=? ORDER BY occurrence_id", (memory_id,)):
                value = dict(item)
                try:
                    value["details"] = json.loads(value.pop("details_json"))
                except ValueError:
                    value["details"] = {"corrupt_details": True}
                    value.pop("details_json", None)
                occurrences.append(value)
            result["occurrences"] = occurrences
            result["transitions"] = [dict(x) for x in conn.execute(
                "SELECT * FROM transitions WHERE memory_id=? ORDER BY transition_id", (memory_id,)
            )]
            relation_rows = conn.execute(
                "SELECT * FROM memory_relations WHERE memory_a_id=? OR memory_b_id=? ORDER BY created_at,relation_id",
                (memory_id, memory_id),
            ).fetchall()
            result["relations"] = [self._decode_relation(x, memory_id) for x in relation_rows]
            outcome_rows = conn.execute(
                "SELECT * FROM memory_outcomes WHERE memory_id=? ORDER BY created_at,outcome_id",
                (memory_id,),
            ).fetchall()
            result["outcomes"] = [self._decode_outcome(x) for x in outcome_rows]
            return result

    def stats(self) -> dict:
        with closing(self._connect()) as conn:
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            occurrences = conn.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0]
            transitions = conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
            relations = conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0]
            outcomes = conn.execute("SELECT COUNT(*) FROM memory_outcomes").fetchone()[0]
            clusters = conn.execute("SELECT COUNT(DISTINCT cluster_key) FROM memories WHERE cluster_key IS NOT NULL").fetchone()[0]
            by_kind = {row[0]: row[1] for row in conn.execute("SELECT kind,COUNT(*) FROM memories GROUP BY kind")}
            by_status = {row[0]: row[1] for row in conn.execute("SELECT status,COUNT(*) FROM memories GROUP BY status")}
            by_category = {row[0]: row[1] for row in conn.execute("SELECT category,COUNT(*) FROM memories GROUP BY category")}
            schema = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
        return {
            "schema_version": int(schema),
            "total_memories": total,
            "total_occurrences": occurrences,
            "total_transitions": transitions,
            "total_relations": relations,
            "total_outcomes": outcomes,
            "total_clusters": clusters,
            "by_kind": by_kind,
            "by_status": by_status,
            "by_category": by_category,
            "database": str(self.db_path),
            "backup": str(self.backup_path),
        }


def learn_run(root: Path, run_id: str, store: MemoryStore | None = None) -> dict:
    root = Path(root).resolve()
    path, artifact_name, data, artifact_sha = _artifact_for_run(root, run_id)
    observations = _extract_observations(artifact_name, data)
    store = store or MemoryStore(root)
    touched: set[str] = set()
    added = 0
    for observation in observations:
        memory_id, inserted = store.upsert(
            observation,
            run_id=run_id,
            artifact_name=artifact_name,
            artifact_sha256=artifact_sha,
            outcome_status=_bounded_text(data.get("status"), 120),
        )
        touched.add(memory_id)
        added += int(inserted)
    return {
        "status": "learned",
        "run_id": run_id,
        "artifact": str(path),
        "artifact_sha256": artifact_sha,
        "observations_created": added,
        "memories_touched": len(touched),
        "memory_ids": sorted(touched),
    }


def _is_bulk_learn_candidate(path: Path) -> bool:
    if path.name.startswith("update_backup_"):
        return False
    return any(os.path.lexists(path / name) for name in ARTIFACT_ORDER)


def learn_all(root: Path, store: MemoryStore | None = None) -> dict:
    root = Path(root).resolve()
    runs = root / "runs"
    store = store or MemoryStore(root)
    learned = skipped = occurrences_added = 0
    errors: list[dict] = []
    if not runs.is_dir():
        return {"status": "learned", "runs_learned": 0, "runs_skipped": 0, "occurrences_added": 0, "errors": []}
    for path in sorted(runs.iterdir(), key=lambda p: p.name):
        if not path.is_dir() or path.is_symlink() or not RUN_ID_RE.fullmatch(path.name):
            continue
        if not _is_bulk_learn_candidate(path):
            skipped += 1
            continue
        try:
            result = learn_run(root, path.name, store)
            if result["memories_touched"]:
                learned += 1
                occurrences_added += result["observations_created"]
            else:
                skipped += 1
        except ValueError as exc:
            skipped += 1
            if len(errors) < 20:
                errors.append({"run_id": path.name, "error": str(exc)[:300]})
    return {
        "status": "learned",
        "runs_learned": learned,
        "runs_skipped": skipped,
        "occurrences_added": occurrences_added,
        "errors": errors,
    }
