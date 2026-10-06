"""Evidence-gated durable knowledge consolidation for SIRA v1.7D-B."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from .models import canonical_url, utc_now

SCHEMA_VERSION = 1
MIN_DISTINCT_SOURCES = 2
MIN_DISTINCT_HOSTS = 2
MIN_CONFIDENCE = 0.80
DEFAULT_STALE_AFTER_DAYS = 180
# A80 policies are explicit per item. Pre-A80 rows keep their original interval.
FRESHNESS_POLICIES = {
    "foundational": 730,
    "stable_engineering": 180,
    "dynamic_current": 7,
    "experience_history": 365,
}
MAX_QUERY_SCAN = 4096
MAX_TRANSITION_REFS = 64
_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{1,127}\Z")
_EVIDENCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{1,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class KnowledgeConsolidationError(OSError):
    pass


def _key(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("knowledge key must be text")
    value = value.strip().casefold()
    if not _KEY_RE.fullmatch(value):
        raise ValueError("invalid knowledge key")
    return value


def _claim(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("claim must be text")
    value = " ".join(unicodedata.normalize("NFKC", value).split())
    if not value or len(value) > 1000:
        raise ValueError("claim must contain 1..1000 characters")
    value.encode("utf-8")
    return value


def _claim_hash(value: str) -> str:
    normalized = _claim(value).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _evidence_id(value: str) -> str:
    if not isinstance(value, str) or not _EVIDENCE_RE.fullmatch(value):
        raise ValueError("invalid evidence id")
    return value


def _sha(value: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError("evidence sha256 must be 64 lowercase hex characters")
    return value


def _confidence(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence must be numeric")
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return round(value, 4)


def _timestamp(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _source(value: str) -> tuple[str, str]:
    url = canonical_url(str(value).strip())
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold().rstrip(".")
    if parts.scheme not in {"http", "https"} or not host:
        raise ValueError("HTTP(S) source URL required")
    if parts.username is not None or parts.password is not None:
        raise ValueError("credential-bearing source URL rejected")
    return url, host


def _age_days(verified_at: str, now: str | None) -> float:
    verified = datetime.fromisoformat(_timestamp(verified_at))
    current = datetime.now(timezone.utc) if now is None else datetime.fromisoformat(_timestamp(now))
    return max(0.0, (current - verified).total_seconds() / 86400.0)


def auto_key(claim_text: str) -> str:
    return "claim." + _claim_hash(claim_text)[:32]


def stable_evidence_id(evidence_sha256: str, claim_text: str, source_url: str) -> str:
    url, _ = _source(source_url)
    material = json.dumps(
        [_sha(evidence_sha256), _claim(claim_text).casefold(), url],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "kve_" + hashlib.sha256(material).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class KnowledgeDecision:
    status: str
    reason: str
    evidence_count: int
    source_count: int
    host_count: int
    confidence: float
    value: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "evidence_count": self.evidence_count,
            "source_count": self.source_count,
            "host_count": self.host_count,
            "confidence": self.confidence,
            "value": self.value,
            "authority_granted": False,
            "promotion_authorized": False,
            "paid_spending_authorized": False,
        }


class KnowledgeConsolidationStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        directory = self.root / "memory"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = directory / "sira_knowledge.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialize(self) -> None:
        try:
            with closing(self._connect()) as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY, value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS knowledge_evidence (
                        evidence_id TEXT NOT NULL,
                        knowledge_key TEXT NOT NULL,
                        claim_text TEXT NOT NULL,
                        claim_hash TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        source_url TEXT NOT NULL,
                        source_host TEXT NOT NULL,
                        confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                        verifier_kind TEXT NOT NULL,
                        evidence_sha256 TEXT NOT NULL,
                        retrieved_at TEXT NOT NULL,
                        recorded_at TEXT NOT NULL,
                        PRIMARY KEY(evidence_id, knowledge_key, source_url)
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_replay
                        ON knowledge_evidence(knowledge_key,claim_hash,evidence_sha256,source_url);
                    CREATE INDEX IF NOT EXISTS idx_knowledge_key
                        ON knowledge_evidence(knowledge_key);
                    CREATE TABLE IF NOT EXISTS consolidated_knowledge (
                        knowledge_key TEXT PRIMARY KEY,
                        claim_text TEXT NOT NULL,
                        claim_hash TEXT NOT NULL,
                        evidence_count INTEGER NOT NULL CHECK(evidence_count >= 1),
                        source_count INTEGER NOT NULL CHECK(source_count >= 1),
                        host_count INTEGER NOT NULL CHECK(host_count >= 1),
                        confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                        last_verified_at TEXT NOT NULL,
                        stale_after_days INTEGER NOT NULL CHECK(stale_after_days >= 1),
                        status TEXT NOT NULL CHECK(status IN ('active','conflicted')),
                        consolidated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS knowledge_lifecycle (
                        knowledge_key TEXT PRIMARY KEY,
                        freshness_class TEXT NOT NULL,
                        policy_days INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        successor_key TEXT,
                        reason TEXT NOT NULL,
                        transitioned_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS knowledge_lifecycle_events (
                        event_id TEXT PRIMARY KEY,
                        knowledge_key TEXT NOT NULL,
                        successor_key TEXT,
                        kind TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        previous_state TEXT NOT NULL,
                        current_state TEXT NOT NULL,
                        evidence_refs_json TEXT NOT NULL,
                        transitioned_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_lifecycle_events_key
                      ON knowledge_lifecycle_events(knowledge_key,transitioned_at);
                """)
                row = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
                if row is None:
                    conn.execute("INSERT INTO metadata(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
                elif row[0] != str(SCHEMA_VERSION):
                    raise KnowledgeConsolidationError("unsupported knowledge schema")
                conn.commit()
        except sqlite3.DatabaseError as exc:
            raise KnowledgeConsolidationError("knowledge store init failed") from exc

    def record_evidence(
        self,
        knowledge_key: str,
        claim_text: str,
        *,
        evidence_id: str,
        source_id: str,
        source_url: str,
        confidence: float,
        verifier_kind: str,
        evidence_sha256: str,
        retrieved_at: str,
        verified: bool,
        cache_replay: bool = False,
    ) -> bool:
        if verified is not True or cache_replay is True:
            return False
        key = _key(knowledge_key)
        claim_text = _claim(claim_text)
        evidence_id = _evidence_id(evidence_id)
        source_id = " ".join(str(source_id).split())
        if not source_id or len(source_id) > 128:
            raise ValueError("invalid source id")
        source_url, source_host = _source(source_url)
        confidence = _confidence(confidence)
        if confidence < MIN_CONFIDENCE:
            return False
        verifier_kind = " ".join(str(verifier_kind).split())[:160]
        if not verifier_kind:
            raise ValueError("verifier kind required")
        evidence_sha256 = _sha(evidence_sha256)
        retrieved_at = _timestamp(retrieved_at)
        try:
            with closing(self._connect()) as conn:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO knowledge_evidence(
                       evidence_id,knowledge_key,claim_text,claim_hash,source_id,
                       source_url,source_host,confidence,verifier_kind,evidence_sha256,
                       retrieved_at,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        evidence_id, key, claim_text, _claim_hash(claim_text), source_id,
                        source_url, source_host, confidence, verifier_kind, evidence_sha256,
                        retrieved_at, utc_now(),
                    ),
                )
                conn.commit()
                return cursor.rowcount == 1
        except sqlite3.DatabaseError as exc:
            raise KnowledgeConsolidationError("knowledge evidence write failed") from exc

    def consolidate(self, knowledge_key: str, *, stale_after_days: int = DEFAULT_STALE_AFTER_DAYS) -> KnowledgeDecision:
        key = _key(knowledge_key)
        if type(stale_after_days) is not int or not 1 <= stale_after_days <= 3650:
            raise ValueError("stale_after_days must be 1..3650")
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    """SELECT e.claim_hash,MIN(e.claim_text) claim_text,
                              COUNT(DISTINCT evidence_id) evidence_count,
                              COUNT(DISTINCT source_url) source_count,
                              COUNT(DISTINCT source_host) host_count,
                              (SELECT AVG(host_confidence) FROM
                                (SELECT MAX(e2.confidence) host_confidence
                                 FROM knowledge_evidence e2
                                 WHERE e2.knowledge_key=? AND e2.claim_hash=e.claim_hash
                                 GROUP BY e2.source_host)) avg_confidence,
                              MAX(retrieved_at) last_verified_at
                       FROM knowledge_evidence e WHERE e.knowledge_key=?
                       GROUP BY e.claim_hash
                       ORDER BY source_count DESC,host_count DESC,avg_confidence DESC,e.claim_hash""",
                    (key, key),
                ).fetchall()
                if not rows:
                    return KnowledgeDecision("pending", "no_verified_evidence", 0, 0, 0, 0.0)
                if len(rows) > 1:
                    counts = conn.execute(
                        "SELECT COUNT(DISTINCT evidence_id),COUNT(DISTINCT source_url),COUNT(DISTINCT source_host) FROM knowledge_evidence WHERE knowledge_key=?",
                        (key,),
                    ).fetchone()
                    existing = conn.execute("SELECT 1 FROM consolidated_knowledge WHERE knowledge_key=?", (key,)).fetchone()
                    if existing is not None:
                        conn.execute(
                            "UPDATE consolidated_knowledge SET status='conflicted',evidence_count=?,source_count=?,host_count=?,consolidated_at=? WHERE knowledge_key=?",
                            (int(counts[0]), int(counts[1]), int(counts[2]), utc_now(), key),
                        )
                        conn.commit()
                    return KnowledgeDecision(
                        "blocked", "conflicting_verified_claims", int(counts[0]), int(counts[1]), int(counts[2]),
                        round(float(rows[0]["avg_confidence"] or 0), 4),
                    )
                row = rows[0]
                evidence_count = int(row["evidence_count"])
                source_count = int(row["source_count"])
                host_count = int(row["host_count"])
                confidence = round(float(row["avg_confidence"] or 0), 4)
                if source_count < MIN_DISTINCT_SOURCES:
                    return KnowledgeDecision("pending", "insufficient_distinct_sources", evidence_count, source_count, host_count, confidence)
                if host_count < MIN_DISTINCT_HOSTS:
                    return KnowledgeDecision("pending", "insufficient_source_independence", evidence_count, source_count, host_count, confidence)
                if confidence < MIN_CONFIDENCE:
                    return KnowledgeDecision("pending", "confidence_below_threshold", evidence_count, source_count, host_count, confidence)
                claim_text = str(row["claim_text"])
                last_verified_at = str(row["last_verified_at"])
                conn.execute(
                    """INSERT INTO consolidated_knowledge(
                       knowledge_key,claim_text,claim_hash,evidence_count,source_count,host_count,
                       confidence,last_verified_at,stale_after_days,status,consolidated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(knowledge_key) DO UPDATE SET
                       claim_text=excluded.claim_text,claim_hash=excluded.claim_hash,
                       evidence_count=excluded.evidence_count,source_count=excluded.source_count,
                       host_count=excluded.host_count,confidence=excluded.confidence,
                       last_verified_at=excluded.last_verified_at,
                       stale_after_days=excluded.stale_after_days,status='active',
                       consolidated_at=excluded.consolidated_at""",
                    (
                        key, claim_text, str(row["claim_hash"]), evidence_count, source_count,
                        host_count, confidence, last_verified_at, stale_after_days, "active", utc_now(),
                    ),
                )
                conn.commit()
                return KnowledgeDecision(
                    "consolidated", "verified_multi_source_claim", evidence_count, source_count, host_count, confidence,
                    {"knowledge_key": key, "claim": claim_text, "last_verified_at": last_verified_at, "stale_after_days": stale_after_days},
                )
        except sqlite3.DatabaseError as exc:
            raise KnowledgeConsolidationError("knowledge consolidation failed") from exc

    @staticmethod
    def _freshness(row: Mapping[str, object], now: str | None) -> dict[str, object]:
        value = dict(row)
        category = value.get("lifecycle_class")
        if value.get("lifecycle_present") is None:
            value["freshness_class"] = "legacy_policy"
            value["freshness_reason"] = "existing_verified_interval"
            value["lifecycle_state"] = "current"
            days = value["stale_after_days"]
        else:
            value["freshness_class"] = category
            value["freshness_reason"] = value.get("lifecycle_reason")
            value["lifecycle_state"] = value.get("lifecycle_state")
            days = value.get("lifecycle_policy_days")
            valid = (isinstance(category, str) and category in FRESHNESS_POLICIES
                     and type(days) is int and days == FRESHNESS_POLICIES[category]
                     and value["lifecycle_state"] in {"current", "superseded", "conflicted", "deprecated"}
                     and isinstance(value["freshness_reason"], str)
                     and 1 <= len(value["freshness_reason"]) <= 200
                     and isinstance(value.get("lifecycle_transitioned_at"), str)
                     and (value["lifecycle_state"] != "superseded" or
                          (isinstance(value.get("successor_key"), str)
                           and bool(_KEY_RE.fullmatch(value["successor_key"]))))
                     and (value["lifecycle_state"] == "superseded" or
                          value.get("successor_key") is None))
            try:
                if valid:
                    _timestamp(value["lifecycle_transitioned_at"])
            except ValueError:
                valid = False
            if not valid:
                value["freshness"] = "invalid_lifecycle_metadata"
                value["revalidation_required"] = True
                value["authority_granted"] = False
                value["promotion_authorized"] = False
                value["paid_spending_authorized"] = False
                return value
        value["effective_stale_after_days"] = days
        try:
            verified = datetime.fromisoformat(_timestamp(str(value["last_verified_at"])))
            value["reconsider_after"] = (verified + timedelta(days=days)).isoformat()
        except (ValueError, OverflowError):
            value["freshness"] = "invalid_lifecycle_metadata"
            value["revalidation_required"] = True
            value["authority_granted"] = False
            value["promotion_authorized"] = False
            value["paid_spending_authorized"] = False
            return value
        if value["status"] == "conflicted" or value["lifecycle_state"] == "conflicted":
            value["freshness"] = "conflicted"
            value["revalidation_required"] = True
        elif value["lifecycle_state"] in {"superseded", "deprecated"}:
            value["freshness"] = value["lifecycle_state"]
            value["revalidation_required"] = False
        else:
            age = _age_days(str(value["last_verified_at"]), now)
            stale = age > days
            value["freshness"] = "stale" if stale else "fresh"
            value["revalidation_required"] = stale
            value["age_days"] = round(age, 3)
        value["authority_granted"] = False
        value["promotion_authorized"] = False
        value["paid_spending_authorized"] = False
        return value

    def _transition_valid(self, value: Mapping[str, object]) -> bool:
        """Check the latest bounded transition against its source rows."""
        if value.get("lifecycle_present") is None:
            return True  # Genuine schema-v1 record with its original verified sources.
        try:
            with closing(self._connect()) as conn:
                event = conn.execute('''SELECT kind,reason,current_state,evidence_refs_json,
                    transitioned_at FROM knowledge_lifecycle_events WHERE knowledge_key=?
                    ORDER BY transitioned_at DESC,event_id DESC LIMIT 1''',
                    (value["knowledge_key"],)).fetchone()
                if (event is None or event["reason"] != value.get("lifecycle_reason")
                        or event["current_state"] != value.get("lifecycle_state")
                        or event["transitioned_at"] != value.get("lifecycle_transitioned_at")):
                    return False
                refs = json.loads(event["evidence_refs_json"])
                if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_TRANSITION_REFS:
                    return False
                fields = ("knowledge_key", "evidence_id", "source_id", "source_url",
                          "source_host", "evidence_sha256", "claim_hash")
                for ref in refs:
                    if not isinstance(ref, dict) or any(not isinstance(ref.get(k), str) for k in fields):
                        return False
                    if conn.execute('''SELECT 1 FROM knowledge_evidence WHERE
                        knowledge_key=? AND evidence_id=? AND source_id=? AND source_url=?
                        AND source_host=? AND evidence_sha256=? AND claim_hash=? LIMIT 1''',
                        tuple(ref[k] for k in fields)).fetchone() is None:
                        return False
                return True
        except (sqlite3.DatabaseError, ValueError, TypeError, UnicodeError):
            return False

    def _checked_freshness(self, row: Mapping[str, object], now: str | None) -> dict[str, object]:
        value = self._freshness(row, now)
        if value["freshness"] != "invalid_lifecycle_metadata" and not self._transition_valid(value):
            value["freshness"] = "invalid_lifecycle_metadata"
            value["revalidation_required"] = True
        return value

    def lookup(self, knowledge_key: str, *, now: str | None = None) -> dict[str, object] | None:
        try:
            with closing(self._connect()) as conn:
                row = conn.execute("""SELECT c.*,l.knowledge_key lifecycle_present,l.freshness_class lifecycle_class,
                    l.policy_days lifecycle_policy_days,l.state lifecycle_state,
                    l.successor_key,l.reason lifecycle_reason,l.transitioned_at lifecycle_transitioned_at
                    FROM consolidated_knowledge c LEFT JOIN knowledge_lifecycle l
                    ON l.knowledge_key=c.knowledge_key WHERE c.knowledge_key=?""",
                    (_key(knowledge_key),)).fetchone()
                return None if row is None else self._checked_freshness(dict(row), now)
        except sqlite3.DatabaseError as exc:
            raise KnowledgeConsolidationError("knowledge lookup failed") from exc

    def verified_source_urls(self, knowledge_key: str, *, limit: int = 4) -> list[str]:
        """Return sources only for a currently active, fresh consolidated claim."""
        key = _key(knowledge_key)
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("source limit must be 1..10")
        current = self.lookup(key)
        if current is None or current["status"] != "active" or current["freshness"] != "fresh":
            return []
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    """SELECT DISTINCT source_url FROM knowledge_evidence
                       WHERE knowledge_key=? AND claim_hash=?
                       ORDER BY source_url LIMIT ?""",
                    (key, current["claim_hash"], limit),
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise KnowledgeConsolidationError("knowledge sources lookup failed") from exc
        return [str(row[0]) for row in rows]

    def verified_source_evidence(self, knowledge_key: str, *, limit: int = 4,
                                 now: str | None = None) -> list[dict[str, object]]:
        """Return bounded exact source references for a current consolidated claim.

        One row per independent host. A stale, conflicted or replaced claim has
        no usable provenance, regardless of its historical evidence rows.
        """
        key = _key(knowledge_key)
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("source limit must be 1..10")
        current = self.lookup(key, now=now)
        if current is None or current["status"] != "active" or current["freshness"] != "fresh":
            return []
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    """SELECT e.evidence_id,e.source_id,e.source_url,e.source_host,
                              e.evidence_sha256,e.verifier_kind,e.retrieved_at
                       FROM knowledge_evidence e
                       WHERE e.knowledge_key=? AND e.claim_hash=? AND e.rowid=(
                           SELECT p.rowid FROM knowledge_evidence p
                           WHERE p.knowledge_key=e.knowledge_key AND p.claim_hash=e.claim_hash
                             AND p.source_host=e.source_host
                           ORDER BY p.retrieved_at DESC,p.rowid DESC LIMIT 1)
                       ORDER BY e.source_host LIMIT ?""",
                    (key, current["claim_hash"], limit),
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise KnowledgeConsolidationError("knowledge provenance lookup failed") from exc
        return [dict(row) for row in rows]

    def verified_claims_for_artifact(self, artifact_sha256: str, *, now: str | None = None) -> list[dict[str, object]]:
        """Find fresh claims supported by two hosts in this exact saved artifact."""
        digest = _sha(artifact_sha256)
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    """SELECT c.*,l.knowledge_key lifecycle_present,l.freshness_class lifecycle_class,
                              l.policy_days lifecycle_policy_days,l.state lifecycle_state,
                              l.successor_key,l.reason lifecycle_reason,l.transitioned_at lifecycle_transitioned_at,
                              COUNT(DISTINCT e.source_host) artifact_hosts
                       FROM consolidated_knowledge c
                       LEFT JOIN knowledge_lifecycle l ON l.knowledge_key=c.knowledge_key
                       JOIN knowledge_evidence e
                         ON e.knowledge_key=c.knowledge_key AND e.claim_hash=c.claim_hash
                       WHERE e.evidence_sha256=? AND c.status='active'
                       GROUP BY c.knowledge_key
                       HAVING COUNT(DISTINCT e.source_host)>=2
                       ORDER BY c.knowledge_key LIMIT 10""",
                    (digest,),
                ).fetchall()
                result = []
                for row in rows:
                    if self._checked_freshness(dict(row), now)["freshness"] != "fresh":
                        continue
                    sources = conn.execute(
                        """SELECT DISTINCT source_url FROM knowledge_evidence
                           WHERE knowledge_key=? AND claim_hash=? AND evidence_sha256=?
                           ORDER BY source_url""",
                        (row["knowledge_key"], row["claim_hash"], digest),
                    ).fetchall()
                    result.append({
                        "knowledge_key": str(row["knowledge_key"]),
                        "claim": str(row["claim_text"]),
                        "source_urls": [str(source[0]) for source in sources],
                    })
        except sqlite3.DatabaseError as exc:
            raise KnowledgeConsolidationError("artifact knowledge lookup failed") from exc
        return result

    def search(self, query: str, limit: int = 10, *, include_stale: bool = False,
               include_conflicted: bool = False, now: str | None = None) -> list[dict[str, object]]:
        query = " ".join(unicodedata.normalize("NFKC", str(query)).casefold().split())
        if not query or len(query) > 500:
            raise ValueError("query must contain 1..500 characters")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be 1..50")
        # A connector word shared with an unrelated claim does not make it
        # relevant. Internal knowledge keys are provenance identifiers, not
        # searchable claim text.
        stopwords = {"a", "about", "an", "and", "are", "as", "at", "be", "both",
                     "by", "for", "from", "in", "is", "it", "of", "on", "or",
                     "the", "to", "with"}
        qtokens = set(re.findall(r"\w+", query, flags=re.UNICODE)) - stopwords
        if not qtokens:
            return []
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT c.*,l.knowledge_key lifecycle_present,l.freshness_class lifecycle_class,l.policy_days lifecycle_policy_days,l.state lifecycle_state,l.successor_key,l.reason lifecycle_reason,l.transitioned_at lifecycle_transitioned_at FROM consolidated_knowledge c LEFT JOIN knowledge_lifecycle l ON l.knowledge_key=c.knowledge_key WHERE c.status IN ('active','conflicted') ORDER BY c.confidence DESC,c.evidence_count DESC,c.knowledge_key LIMIT ?"
                    if include_conflicted else
                    "SELECT c.*,l.knowledge_key lifecycle_present,l.freshness_class lifecycle_class,l.policy_days lifecycle_policy_days,l.state lifecycle_state,l.successor_key,l.reason lifecycle_reason,l.transitioned_at lifecycle_transitioned_at FROM consolidated_knowledge c LEFT JOIN knowledge_lifecycle l ON l.knowledge_key=c.knowledge_key WHERE c.status='active' ORDER BY c.confidence DESC,c.evidence_count DESC,c.knowledge_key LIMIT ?",
                    (MAX_QUERY_SCAN,),
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise KnowledgeConsolidationError("knowledge search failed") from exc
        result = []
        for row in rows:
            value = self._freshness(dict(row), now)
            claim = unicodedata.normalize("NFKC", str(value["claim_text"])).casefold()
            tokens = set(re.findall(r"\w+", claim, flags=re.UNICODE)) - stopwords
            overlap = len(qtokens & tokens)
            if not overlap:
                continue
            value = self._checked_freshness(dict(row), now)
            if value["freshness"] != "fresh" and not include_stale:
                continue
            value["retrieval_score"] = round(min(1.0, overlap / max(1, len(qtokens)) * .7 + float(value["confidence"]) * .3), 4)
            result.append(value)
        result.sort(key=lambda row: (float(row["retrieval_score"]), float(row["confidence"]), int(row["evidence_count"]), str(row["knowledge_key"])), reverse=True)
        return result[:limit]

    def revalidation_candidates(self, limit: int = 20, *, now: str | None = None) -> list[dict[str, object]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    """SELECT c.*,l.knowledge_key lifecycle_present,l.freshness_class lifecycle_class,l.policy_days lifecycle_policy_days,
                       l.state lifecycle_state,l.successor_key,l.reason lifecycle_reason,
                       l.transitioned_at lifecycle_transitioned_at
                       FROM consolidated_knowledge c LEFT JOIN knowledge_lifecycle l
                         ON l.knowledge_key=c.knowledge_key
                       ORDER BY c.last_verified_at,c.knowledge_key LIMIT ?""",
                    (MAX_QUERY_SCAN,),
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise KnowledgeConsolidationError("knowledge revalidation query failed") from exc
        result = [self._checked_freshness(dict(row), now) for row in rows]
        return [row for row in result if row["revalidation_required"]][:limit]

    def stats(self) -> dict[str, object]:
        try:
            with closing(self._connect()) as conn:
                return {
                    "schema_version": int(conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]),
                    "knowledge_evidence": int(conn.execute("SELECT COUNT(*) FROM knowledge_evidence").fetchone()[0]),
                    "active_knowledge": int(conn.execute("SELECT COUNT(*) FROM consolidated_knowledge WHERE status='active'").fetchone()[0]),
                    "conflicted_knowledge": int(conn.execute("SELECT COUNT(*) FROM consolidated_knowledge WHERE status='conflicted'").fetchone()[0]),
                    "current_knowledge": int(conn.execute("""SELECT COUNT(*) FROM consolidated_knowledge c
                        LEFT JOIN knowledge_lifecycle l ON l.knowledge_key=c.knowledge_key
                        WHERE c.status='active' AND (l.state IS NULL OR l.state='current')""").fetchone()[0]),
                    "superseded_knowledge": int(conn.execute("SELECT COUNT(*) FROM knowledge_lifecycle WHERE state='superseded'").fetchone()[0]),
                    "lifecycle_conflicts": int(conn.execute("SELECT COUNT(*) FROM knowledge_lifecycle WHERE state='conflicted'").fetchone()[0]),
                    "deprecated_knowledge": int(conn.execute("SELECT COUNT(*) FROM knowledge_lifecycle WHERE state='deprecated'").fetchone()[0]),
                    "database": str(self.db_path),
                    "authority_granted": False,
                    "promotion_authorized": False,
                    "paid_spending_authorized": False,
                }
        except sqlite3.DatabaseError as exc:
            raise KnowledgeConsolidationError("knowledge stats failed") from exc


def record_verified_answer_claims(
    root: Path,
    *,
    evidence_sha256: str,
    accepted_claims: Iterable[Mapping[str, object]],
    sources: Iterable[Mapping[str, object]],
    verified_at: str,
    cache_replay: bool,
) -> dict[str, object]:
    if cache_replay:
        return {
            "status": "cache_replay_skipped", "evidence_recorded": 0, "duplicate_evidence": 0,
            "consolidated_claims": 0, "pending_claims": 0, "blocked_claims": 0, "decisions": [],
            "authority_granted": False, "promotion_authorized": False, "paid_spending_authorized": False,
        }
    evidence_sha256 = _sha(evidence_sha256)
    verified_at = _timestamp(verified_at)
    source_map = {
        str(source.get("source_id")): source
        for source in sources
        if isinstance(source, Mapping) and isinstance(source.get("source_id"), str)
    }
    store = KnowledgeConsolidationStore(Path(root).resolve())
    inserted = duplicates = 0
    decisions = []
    for claim in accepted_claims:
        if not isinstance(claim, Mapping) or not isinstance(claim.get("text"), str) or not isinstance(claim.get("source_ids"), list):
            continue
        text = _claim(str(claim["text"]))
        key = auto_key(text)
        for source_id in dict.fromkeys(x for x in claim["source_ids"] if isinstance(x, str)):
            source = source_map.get(source_id)
            if not isinstance(source, Mapping) or not isinstance(source.get("url"), str):
                continue
            url, _ = _source(str(source["url"]))
            eid = stable_evidence_id(evidence_sha256, text, url)
            if store.record_evidence(
                key, text, evidence_id=eid, source_id=source_id, source_url=url,
                confidence=.85, verifier_kind="synthesis_same_model_second_pass_local_gate",
                evidence_sha256=evidence_sha256, retrieved_at=verified_at, verified=True,
            ):
                inserted += 1
            else:
                duplicates += 1
        decisions.append(store.consolidate(key).to_dict())
    return {
        "status": "completed",
        "evidence_recorded": inserted,
        "duplicate_evidence": duplicates,
        "consolidated_claims": sum(row["status"] == "consolidated" for row in decisions),
        "pending_claims": sum(row["status"] == "pending" for row in decisions),
        "blocked_claims": sum(row["status"] == "blocked" for row in decisions),
        "decisions": decisions,
        "authority_granted": False,
        "promotion_authorized": False,
        "paid_spending_authorized": False,
    }


def safe_record_verified_answer_claims(root: Path, **kwargs) -> dict[str, object]:
    try:
        return record_verified_answer_claims(root, **kwargs)
    except (KnowledgeConsolidationError, OSError, ValueError, TypeError, UnicodeError) as exc:
        return {
            "status": "learning_error", "reason": type(exc).__name__,
            "evidence_recorded": 0, "duplicate_evidence": 0,
            "consolidated_claims": 0, "pending_claims": 0, "blocked_claims": 0,
            "decisions": [], "authority_granted": False,
            "promotion_authorized": False, "paid_spending_authorized": False,
        }
