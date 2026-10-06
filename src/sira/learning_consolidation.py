"""Evidence-gated local language and reusable-skill consolidation."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from typing import Iterable

from .models import utc_now

SCHEMA_VERSION = 1
MIN_MAPPING_EVIDENCE = 2
MIN_MAPPING_CONFIDENCE = 0.80
MIN_SKILL_SUCCESSES = 3
MIN_SKILL_CONFIDENCE = 0.75
EVIDENCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{1,127}\Z")
SKILL_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{1,95}\Z")


class LearningConsolidationError(OSError):
    pass


def _token(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("token must be text")
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    if not value or len(value) > 64 or any(c.isspace() for c in value):
        raise ValueError("token must be a bounded single token")
    value.encode("utf-8")
    return value


def _meaning(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("meaning must be text")
    value = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not value or len(value) > 160:
        raise ValueError("meaning must contain 1..160 characters")
    return value


def _evidence(value: str) -> str:
    if not isinstance(value, str) or not EVIDENCE_ID_RE.fullmatch(value):
        raise ValueError("invalid evidence id")
    return value


def _confidence(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence must be numeric")
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return round(value, 4)


def _skill_key(value: str) -> str:
    if not isinstance(value, str) or not SKILL_KEY_RE.fullmatch(value.strip().casefold()):
        raise ValueError("invalid skill key")
    return value.strip().casefold()


def _title(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("title must be text")
    value = " ".join(value.split())
    if not value or len(value) > 200:
        raise ValueError("title must contain 1..200 characters")
    return value


def _steps(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("steps must be a sequence")
    rows = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("step must be text")
        clean = " ".join(value.split())
        if not clean or len(clean) > 600:
            raise ValueError("step must contain 1..600 characters")
        rows.append(clean)
    if not 1 <= len(rows) <= 12:
        raise ValueError("skill requires 1..12 steps")
    return tuple(rows)


def _steps_hash(values: tuple[str, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ConsolidationDecision:
    status: str
    reason: str
    evidence_count: int
    confidence: float
    value: dict[str, object] | None = None

    def to_dict(self):
        return {
            "status": self.status,
            "reason": self.reason,
            "evidence_count": self.evidence_count,
            "confidence": self.confidence,
            "value": self.value,
            "authority_granted": False,
            "promotion_authorized": False,
            "paid_spending_authorized": False,
        }


class LearningConsolidationStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        directory = self.root / "memory"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = directory / "sira_consolidation.sqlite3"
        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialize(self):
        try:
            with closing(self._connect()) as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY, value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS language_mapping_evidence (
                        evidence_id TEXT NOT NULL,
                        token TEXT NOT NULL,
                        meaning TEXT NOT NULL,
                        confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                        verifier TEXT NOT NULL,
                        recorded_at TEXT NOT NULL,
                        PRIMARY KEY(evidence_id,token,meaning)
                    );
                    CREATE INDEX IF NOT EXISTS idx_language_mapping_token
                        ON language_mapping_evidence(token);
                    CREATE TABLE IF NOT EXISTS consolidated_language_mappings (
                        token TEXT PRIMARY KEY,
                        meaning TEXT NOT NULL,
                        evidence_count INTEGER NOT NULL CHECK(evidence_count >= 2),
                        confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                        consolidated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS skill_evidence (
                        evidence_id TEXT NOT NULL,
                        skill_key TEXT NOT NULL,
                        title TEXT NOT NULL,
                        steps_json TEXT NOT NULL,
                        steps_hash TEXT NOT NULL,
                        succeeded INTEGER NOT NULL CHECK(succeeded IN (0,1)),
                        confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                        source_kind TEXT NOT NULL,
                        recorded_at TEXT NOT NULL,
                        PRIMARY KEY(evidence_id,skill_key)
                    );
                    CREATE INDEX IF NOT EXISTS idx_skill_evidence_key
                        ON skill_evidence(skill_key);
                    CREATE TABLE IF NOT EXISTS consolidated_skills (
                        skill_key TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        steps_json TEXT NOT NULL,
                        steps_hash TEXT NOT NULL,
                        evidence_count INTEGER NOT NULL CHECK(evidence_count >= 3),
                        confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                        status TEXT NOT NULL CHECK(status IN ('active','retired')),
                        consolidated_at TEXT NOT NULL
                    );
                """)
                row = conn.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
                        (str(SCHEMA_VERSION),),
                    )
                elif row[0] != str(SCHEMA_VERSION):
                    raise LearningConsolidationError("unsupported consolidation schema")
                conn.commit()
        except sqlite3.DatabaseError as exc:
            raise LearningConsolidationError("consolidation store init failed") from exc

    def record_language_mapping(self, token: str, meaning: str, *, evidence_id: str,
                                confidence: float, verifier: str, verified: bool) -> bool:
        if verified is not True:
            return False
        token, meaning = _token(token), _meaning(meaning)
        evidence_id, confidence = _evidence(evidence_id), _confidence(confidence)
        if confidence < MIN_MAPPING_CONFIDENCE:
            return False
        verifier = " ".join(str(verifier).split())[:120]
        if not verifier:
            raise ValueError("verifier is required")
        try:
            with closing(self._connect()) as conn:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO language_mapping_evidence(
                       evidence_id,token,meaning,confidence,verifier,recorded_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (evidence_id, token, meaning, confidence, verifier, utc_now()),
                )
                conn.commit()
                return cursor.rowcount == 1
        except sqlite3.DatabaseError as exc:
            raise LearningConsolidationError("language mapping write failed") from exc

    def consolidate_language_mapping(self, token: str) -> ConsolidationDecision:
        token = _token(token)
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    """SELECT meaning,COUNT(DISTINCT evidence_id) evidence_count,
                              AVG(confidence) avg_confidence
                       FROM language_mapping_evidence
                       WHERE token=?
                       GROUP BY meaning
                       ORDER BY evidence_count DESC,avg_confidence DESC,meaning""",
                    (token,),
                ).fetchall()
                if not rows:
                    return ConsolidationDecision("pending","no_verified_evidence",0,0.0)
                if len(rows) > 1:
                    return ConsolidationDecision(
                        "blocked","conflicting_verified_meanings",
                        sum(int(r["evidence_count"]) for r in rows),
                        round(float(rows[0]["avg_confidence"] or 0),4),
                    )
                row = rows[0]
                count = int(row["evidence_count"])
                confidence = round(float(row["avg_confidence"] or 0),4)
                if count < MIN_MAPPING_EVIDENCE:
                    return ConsolidationDecision("pending","insufficient_distinct_evidence",count,confidence)
                if confidence < MIN_MAPPING_CONFIDENCE:
                    return ConsolidationDecision("pending","confidence_below_threshold",count,confidence)
                meaning = str(row["meaning"])
                conn.execute(
                    """INSERT INTO consolidated_language_mappings(
                       token,meaning,evidence_count,confidence,consolidated_at
                       ) VALUES(?,?,?,?,?)
                       ON CONFLICT(token) DO UPDATE SET
                       meaning=excluded.meaning,evidence_count=excluded.evidence_count,
                       confidence=excluded.confidence,consolidated_at=excluded.consolidated_at""",
                    (token, meaning, count, confidence, utc_now()),
                )
                conn.commit()
                return ConsolidationDecision(
                    "consolidated","verified_repeated_mapping",count,confidence,
                    {"token":token,"meaning":meaning},
                )
        except sqlite3.DatabaseError as exc:
            raise LearningConsolidationError("language mapping consolidation failed") from exc

    def active_lexicon(self) -> dict[str,str]:
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT token,meaning FROM consolidated_language_mappings ORDER BY token"
                ).fetchall()
                return {str(r["token"]):str(r["meaning"]) for r in rows}
        except sqlite3.DatabaseError as exc:
            raise LearningConsolidationError("lexicon read failed") from exc

    def record_skill_evidence(self, skill_key: str, title: str, steps: Iterable[str], *,
                              evidence_id: str, succeeded: bool, confidence: float,
                              source_kind: str) -> bool:
        skill_key, title, steps = _skill_key(skill_key), _title(title), _steps(steps)
        evidence_id, confidence = _evidence(evidence_id), _confidence(confidence)
        if type(succeeded) is not bool:
            raise ValueError("succeeded must be boolean")
        source_kind = " ".join(str(source_kind).split())[:120]
        if not source_kind:
            raise ValueError("source kind is required")
        encoded = json.dumps(steps, ensure_ascii=False, separators=(",",":"))
        digest = _steps_hash(steps)
        try:
            with closing(self._connect()) as conn:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO skill_evidence(
                       evidence_id,skill_key,title,steps_json,steps_hash,succeeded,
                       confidence,source_kind,recorded_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (evidence_id,skill_key,title,encoded,digest,int(succeeded),
                     confidence,source_kind,utc_now()),
                )
                conn.commit()
                return cursor.rowcount == 1
        except sqlite3.DatabaseError as exc:
            raise LearningConsolidationError("skill evidence write failed") from exc

    def consolidate_skill(self, skill_key: str) -> ConsolidationDecision:
        skill_key = _skill_key(skill_key)
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    """SELECT steps_hash,steps_json,title,
                              SUM(CASE WHEN succeeded=1 THEN 1 ELSE 0 END) successes,
                              SUM(CASE WHEN succeeded=0 THEN 1 ELSE 0 END) failures,
                              AVG(CASE WHEN succeeded=1 THEN confidence END) avg_confidence
                       FROM skill_evidence WHERE skill_key=?
                       GROUP BY steps_hash,steps_json,title
                       ORDER BY successes DESC,avg_confidence DESC,steps_hash""",
                    (skill_key,),
                ).fetchall()
                if not rows:
                    return ConsolidationDecision("pending","no_skill_evidence",0,0.0)
                total = sum(int(r["successes"] or 0)+int(r["failures"] or 0) for r in rows)
                if len({str(r["steps_hash"]) for r in rows}) > 1:
                    return ConsolidationDecision(
                        "blocked","conflicting_skill_procedures",total,
                        round(float(rows[0]["avg_confidence"] or 0),4),
                    )
                row = rows[0]
                successes, failures = int(row["successes"] or 0), int(row["failures"] or 0)
                confidence = round(float(row["avg_confidence"] or 0),4)
                if failures:
                    return ConsolidationDecision(
                        "blocked","failed_application_evidence_present",
                        successes+failures,confidence,
                    )
                if successes < MIN_SKILL_SUCCESSES:
                    return ConsolidationDecision(
                        "pending","insufficient_successful_applications",successes,confidence
                    )
                if confidence < MIN_SKILL_CONFIDENCE:
                    return ConsolidationDecision(
                        "pending","skill_confidence_below_threshold",successes,confidence
                    )
                conn.execute(
                    """INSERT INTO consolidated_skills(
                       skill_key,title,steps_json,steps_hash,evidence_count,
                       confidence,status,consolidated_at
                       ) VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(skill_key) DO UPDATE SET
                       title=excluded.title,steps_json=excluded.steps_json,
                       steps_hash=excluded.steps_hash,evidence_count=excluded.evidence_count,
                       confidence=excluded.confidence,status='active',
                       consolidated_at=excluded.consolidated_at""",
                    (skill_key,str(row["title"]),str(row["steps_json"]),str(row["steps_hash"]),
                     successes,confidence,"active",utc_now()),
                )
                conn.commit()
                return ConsolidationDecision(
                    "consolidated","repeated_successful_procedure",successes,confidence,
                    {"skill_key":skill_key,"title":str(row["title"]),
                     "steps":json.loads(row["steps_json"])},
                )
        except sqlite3.DatabaseError as exc:
            raise LearningConsolidationError("skill consolidation failed") from exc

    def list_skills(self):
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    """SELECT * FROM consolidated_skills WHERE status='active'
                       ORDER BY confidence DESC,evidence_count DESC,skill_key"""
                ).fetchall()
                return [{
                    "skill_key":r["skill_key"],"title":r["title"],
                    "steps":json.loads(r["steps_json"]),
                    "evidence_count":int(r["evidence_count"]),
                    "confidence":float(r["confidence"]),"status":r["status"],
                    "consolidated_at":r["consolidated_at"],
                    "authority_granted":False,"promotion_authorized":False,
                } for r in rows]
        except (sqlite3.DatabaseError,json.JSONDecodeError) as exc:
            raise LearningConsolidationError("skill read failed") from exc

    def stats(self):
        try:
            with closing(self._connect()) as conn:
                schema = int(conn.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0])
                return {
                    "schema_version":schema,
                    "language_mapping_evidence":int(conn.execute(
                        "SELECT COUNT(*) FROM language_mapping_evidence"
                    ).fetchone()[0]),
                    "consolidated_language_mappings":int(conn.execute(
                        "SELECT COUNT(*) FROM consolidated_language_mappings"
                    ).fetchone()[0]),
                    "skill_evidence":int(conn.execute(
                        "SELECT COUNT(*) FROM skill_evidence"
                    ).fetchone()[0]),
                    "active_skills":int(conn.execute(
                        "SELECT COUNT(*) FROM consolidated_skills WHERE status='active'"
                    ).fetchone()[0]),
                    "database":str(self.db_path),
                    "authority_granted":False,
                    "promotion_authorized":False,
                }
        except sqlite3.DatabaseError as exc:
            raise LearningConsolidationError("consolidation stats failed") from exc
