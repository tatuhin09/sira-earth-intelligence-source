"""A81 small offline bootstrap: raw snapshots -> candidates -> A80 verified memory.

The corpus is data, never code or instructions. This bounded local pilot has no
network, model, skill, permission, or promotion operations. A manifest's tier is
curator metadata, not proof that an arbitrary downloaded page is authoritative.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .knowledge_consolidation import (FRESHNESS_POLICIES, KnowledgeConsolidationStore,
                                      auto_key, stable_evidence_id, _timestamp)
from .knowledge_evolution import KnowledgeEvolutionStore
from .models import utc_now
from .storage import write_json

MAX_SOURCE_BYTES = 65_536
MAX_SNAPSHOTS = 24
MAX_CLAIMS_PER_SOURCE = 8
MAX_CANDIDATES = MAX_SNAPSHOTS * MAX_CLAIMS_PER_SOURCE
MAX_CORPUS_BYTES = 1_048_576
_ID = re.compile(r"[a-z][a-z0-9_-]{2,63}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_UNSAFE_CLAIM = re.compile(
    r"\b(ignore (previous|all) instructions|execute (a |the )?(shell|command)|"
    r"activate (a |the )?skill|grant authority|authorize promotion)\b", re.I)
_SECRET_MARKER = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    rb"\b(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*[A-Za-z0-9._/+\-=]{12,}", re.I)
_SOURCE_TYPES = {"official_documentation", "technical_standard", "academic_standard",
                 "scholarly_publication", "public_technical_repository", "secondary_web"}


def _bounded_text(value: object, name: str, limit: int, *, minimum: int = 1) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= limit or "\x00" in value:
        raise ValueError(f"Invalid {name}")
    return value


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_no_duplicates(data: bytes) -> dict:
    def pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise ValueError("Duplicate corpus JSON key")
            output[key] = value
        return output
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid UTF-8 corpus JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Corpus must be a structured object")
    return value


def _opposition(left: str, right: str) -> bool:
    def normalized(value):
        tokens = re.findall(r"[a-z0-9]+", value.casefold())
        polarity = any(token in {"no", "not", "never"} for token in tokens)
        base = ["find" if token in {"finds", "find"} else token for token in tokens
                if token not in {"no", "not", "never", "does", "do"}]
        return base, polarity
    a, negative_a = normalized(left)
    b, negative_b = normalized(right)
    return a == b and negative_a != negative_b


def _timestamp_age(value: str, now: datetime) -> float:
    dt = datetime.fromisoformat(_timestamp(value))
    return (now - dt).total_seconds()


class BootstrapKnowledgeBuilder:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.base = self.root / "memory" / "bootstrap_corpus"
        self.manifests = self.base / "manifests"
        self.raw = self.base / "raw"
        self.candidates = self.base / "candidates"
        # Existing hostile symlinks must not redirect corpus writes.
        for path in (self.root / "memory", self.base, self.manifests, self.raw, self.candidates):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ValueError("Unsafe corpus directory")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _name(value: str) -> str:
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError("Invalid bootstrap identifier")
        return value

    def _entries(self, folder: Path, limit: int) -> list[Path]:
        rows = []
        for row in folder.iterdir():
            rows.append(row)
            if len(rows) > limit:
                raise ValueError("Corpus record bound exceeded")
        if any(row.is_symlink() or not row.is_file() or row.suffix != ".json"
                                     for row in rows):
            raise ValueError("Corpus record bound or path integrity failed")
        return sorted(rows)

    @staticmethod
    def _read(path: Path, max_bytes: int) -> bytes:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
            raise ValueError("Missing, unsafe, or oversized corpus record")
        data = path.read_bytes()
        if len(data) > max_bytes:
            raise ValueError("Corpus record changed during read")
        return data

    def _manifest(self, snapshot_id: str) -> dict:
        path = self.manifests / (self._name(snapshot_id) + ".json")
        manifest = _json_no_duplicates(self._read(path, 8192))
        if manifest.get("snapshot_id") != snapshot_id or manifest.get("raw_path") != str(self.raw / (snapshot_id + ".json")):
            raise ValueError("Manifest identity or raw path mismatch")
        checksum = manifest.pop("manifest_sha256", None)
        if not isinstance(checksum, str) or checksum != _digest(json.dumps(
                manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()):
            raise ValueError("Manifest metadata integrity mismatch")
        manifest["manifest_sha256"] = checksum
        digest = manifest.get("content_sha256")
        if not isinstance(digest, str) or not _HEX.fullmatch(digest):
            raise ValueError("Invalid manifest digest")
        raw = self._read(self.raw / (snapshot_id + ".json"), MAX_SOURCE_BYTES)
        if _digest(raw) != digest or len(raw) != manifest.get("size_bytes"):
            raise ValueError("Raw snapshot hash mismatch")
        return manifest

    def ingest_source(self, metadata: dict, raw: bytes, *, expected_sha256: str) -> dict:
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_SOURCE_BYTES:
            raise ValueError("Source size exceeds bounded pilot limit")
        if not isinstance(expected_sha256, str) or not _HEX.fullmatch(expected_sha256) or _digest(raw) != expected_sha256:
            raise ValueError("Source SHA-256 mismatch")
        if _SECRET_MARKER.search(raw):
            raise ValueError("Credential-like source material cannot enter bootstrap corpus")
        if not isinstance(metadata, dict):
            raise ValueError("Manifest metadata required")
        source_id = self._name(metadata.get("source_id"))
        name = _bounded_text(metadata.get("canonical_name"), "canonical name", 120)
        kind = metadata.get("source_type")
        tier = metadata.get("trust_tier")
        if kind not in _SOURCE_TYPES or type(tier) is not int or tier not in (1, 2, 3, 4):
            raise ValueError("Invalid source class or trust tier")
        url = _bounded_text(metadata.get("canonical_origin"), "canonical origin", 512)
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or not re.fullmatch(r"[A-Za-z0-9.-]+", parts.hostname)):
            raise ValueError("Origin must be a credential-free canonical HTTPS URL")
        if tier == 1 and kind not in {"official_documentation", "technical_standard"}:
            raise ValueError("Tier 1 source class mismatch")
        if tier == 2 and kind not in {"academic_standard", "scholarly_publication"}:
            raise ValueError("Tier 2 source class mismatch")
        if tier == 3 and kind != "public_technical_repository":
            raise ValueError("Tier 3 source class mismatch")
        if tier == 4 and kind != "secondary_web":
            raise ValueError("Tier 4 source class mismatch")
        for field, size in (("domain", 100), ("version", 80), ("license", 160)):
            _bounded_text(metadata.get(field), field, size)
        if metadata.get("parser_type") != "structured_claims_v1":
            raise ValueError("Unsupported bounded parser")
        freshness = metadata.get("freshness_class")
        if freshness not in FRESHNESS_POLICIES:
            raise ValueError("Unknown freshness policy")
        provenance = metadata.get("provenance_status")
        if provenance not in {"unreviewed", "curated_snapshot_reviewed"}:
            raise ValueError("Invalid provenance status")
        reason = metadata.get("verification_reason")
        if provenance == "curated_snapshot_reviewed":
            _bounded_text(reason, "verification reason", 200, minimum=10)
        elif reason != "":
            raise ValueError("Unreviewed source cannot have a verification reason")
        retrieved_at = _timestamp(metadata.get("retrieved_at"))
        parsed = _json_no_duplicates(raw)
        if parsed.get("seed_mode") == "curated_preverified" and provenance != "curated_snapshot_reviewed":
            raise ValueError("Curated seed requires explicit reviewed provenance")
        self._claim_rows(parsed)  # Bound and validate before any write.
        snapshot_id = "bs_" + _digest(json.dumps(
            [source_id, metadata["version"], expected_sha256], separators=(",", ":")
        ).encode())[:32]
        existing = self._entries(self.manifests, MAX_SNAPSHOTS)
        for path in existing:
            old = self._manifest(path.stem)
            if old["source_id"] == source_id and (old["canonical_origin"] != url
                    or old["source_type"] != kind or old["trust_tier"] != tier):
                raise ValueError("Source identity changed across snapshots")
            if old["snapshot_id"] == snapshot_id:
                if old["content_sha256"] != expected_sha256 or old["version"] != metadata["version"]:
                    raise ValueError("Snapshot identity collision")
                return old
        if len(existing) >= MAX_SNAPSHOTS or sum(p.stat().st_size for p in self._entries(self.raw, MAX_SNAPSHOTS)) + len(raw) > MAX_CORPUS_BYTES:
            raise ValueError("Bootstrap corpus capacity reached")
        manifest = {"schema": "sira.bootstrap_manifest.v1", "snapshot_id": snapshot_id,
                    "source_id": source_id, "canonical_name": name, "source_type": kind,
                    "trust_tier": tier, "domain": metadata["domain"], "canonical_origin": url,
                    "version": metadata["version"], "license": metadata["license"],
                    "parser_type": "structured_claims_v1", "freshness_class": freshness,
                    "provenance_status": provenance, "verification_reason": reason,
                    "retrieved_at": retrieved_at, "imported_at": utc_now(),
                    "content_sha256": expected_sha256, "size_bytes": len(raw),
                    "raw_path": str(self.raw / (snapshot_id + ".json"))}
        manifest["manifest_sha256"] = _digest(json.dumps(
            manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())
        raw_path = self.raw / (snapshot_id + ".json")
        manifest_path = self.manifests / (snapshot_id + ".json")
        # Exclusive creates prevent accidental overwrite of immutable snapshots.
        with raw_path.open("xb") as out:
            out.write(raw)
        raw_path.chmod(0o600)
        write_json(manifest_path, manifest)
        manifest_path.chmod(0o600)
        return manifest

    @staticmethod
    def _claim_rows(parsed: dict) -> list[dict]:
        rows = parsed.get("claims")
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_CLAIMS_PER_SOURCE:
            raise ValueError("Bounded structured claims required")
        output = []
        for row in rows:
            if not isinstance(row, dict) or row.get("claim_type") not in {"technical_fact", "narrow_procedure"}:
                raise ValueError("Invalid structured claim")
            claim = _bounded_text(row.get("claim"), "claim", 300, minimum=20)
            excerpt = _bounded_text(row.get("excerpt"), "source excerpt", 400, minimum=20)
            topic = _bounded_text(row.get("topic"), "topic", 100, minimum=3)
            if claim != excerpt or _UNSAFE_CLAIM.search(claim):
                raise ValueError("Claim must be exact narrow excerpt, not an instruction")
            output.append({"claim": claim, "excerpt": excerpt, "topic": topic,
                           "claim_type": row["claim_type"]})
        return output

    def candidate_path(self, candidate_id: str) -> Path:
        return self.candidates / (self._name(candidate_id) + ".json")

    def _expected_candidates(self, manifest: dict) -> list[dict]:
        raw = self._read(self.raw / (manifest["snapshot_id"] + ".json"), MAX_SOURCE_BYTES)
        rows = self._claim_rows(_json_no_duplicates(raw))
        result = []
        for index, row in enumerate(rows):
            candidate_id = "bc_" + _digest(json.dumps(
                [manifest["snapshot_id"], index, row], sort_keys=True,
                separators=(",", ":"), ensure_ascii=False).encode())[:32]
            result.append({"schema": "sira.bootstrap_candidate.v1", "candidate_id": candidate_id,
                           "snapshot_id": manifest["snapshot_id"], "source_id": manifest["source_id"],
                           "source_sha256": manifest["content_sha256"], "source_url": manifest["canonical_origin"],
                           "source_tier": manifest["trust_tier"], "retrieved_at": manifest["retrieved_at"],
                           "freshness_class": manifest["freshness_class"],
                           "verification_status": "candidate", "extractor": "structured_exact_excerpt_v1",
                           "index": index, **row})
        return result

    def extract_candidates(self, snapshot_id: str) -> list[dict]:
        manifest = self._manifest(snapshot_id)
        rows = self._expected_candidates(manifest)
        existing = self._entries(self.candidates, MAX_CANDIDATES)
        missing = [row for row in rows if not self.candidate_path(row["candidate_id"]).exists()]
        if len(existing) + len(missing) > MAX_CANDIDATES:
            raise ValueError("Candidate capacity reached")
        for row in rows:
            path = self.candidate_path(row["candidate_id"])
            if path.exists():
                if _json_no_duplicates(self._read(path, 8192)) != row:
                    raise ValueError("Candidate record was modified")
            else:
                write_json(path, row)
                path.chmod(0o600)
        return rows

    def _candidate(self, candidate_id: str) -> tuple[dict, dict]:
        path = self.candidate_path(candidate_id)
        row = _json_no_duplicates(self._read(path, 8192))
        manifest = self._manifest(row.get("snapshot_id"))
        expected = self._expected_candidates(manifest)
        if row not in expected or row.get("candidate_id") != candidate_id:
            raise ValueError("Candidate provenance mismatch")
        return row, manifest

    def assess_candidate(self, candidate_id: str, *, now: str | None = None) -> dict:
        row, manifest = self._candidate(candidate_id)
        current = datetime.now(timezone.utc) if now is None else datetime.fromisoformat(_timestamp(now))
        peers = []
        for path in self._entries(self.candidates, MAX_CANDIDATES):
            peer, source = self._candidate(path.stem)
            if peer["topic"].casefold() == row["topic"].casefold():
                peers.append((peer, source))
        result = {"schema": "sira.bootstrap_assessment.v1", "candidate_id": candidate_id,
                  "claim": row["claim"], "status": "insufficient_evidence",
                  "reason": "two_independent_reviewed_hosts_required", "support": [],
                  "contradictions": [], "verification_policy": "exact_excerpt_two_reviewed_hosts_v1",
                  "knowledge_key": auto_key(row["claim"]), "api_requests": 0,
                  "metered_model_requests": 0, "skill_activated": False}
        if manifest["provenance_status"] != "curated_snapshot_reviewed":
            return result
        supporters = [(peer, source) for peer, source in peers if peer["claim"] == row["claim"]
                      and source["provenance_status"] == "curated_snapshot_reviewed"]
        opposites = [(peer, source) for peer, source in peers if _opposition(row["claim"], peer["claim"])
                     and source["provenance_status"] == "curated_snapshot_reviewed"]
        if opposites:
            result.update(status="contradictory_evidence", reason="opposing_reviewed_snapshot",
                          contradictions=[{"candidate_id": peer["candidate_id"],
                                           "snapshot_id": source["snapshot_id"]}
                                          for peer, source in opposites[:8]])
            return result
        def is_fresh(source):
            age = _timestamp_age(source["retrieved_at"], current)
            return -300 <= age <= FRESHNESS_POLICIES[source["freshness_class"]] * 86400
        if not is_fresh(manifest):
            result.update(status="stale_evidence", reason="snapshot_requires_revalidation")
            return result
        supporters = [(peer, source) for peer, source in supporters if is_fresh(source)]
        # One physical host contributes once, regardless of versions or aliases.
        distinct = {}
        for peer, source in sorted(supporters, key=lambda pair: (
                pair[1]["retrieved_at"], pair[1]["snapshot_id"] == row["snapshot_id"],
                pair[0]["candidate_id"]), reverse=True):
            distinct.setdefault(urlsplit(source["canonical_origin"]).hostname, (peer, source))
        if len(distinct) < 2:
            return result
        if not any(source["trust_tier"] <= 2 for peer, source in distinct.values()):
            result["reason"] = "primary_or_scholarly_source_required"
            return result
        # Prohibit different freshness policies for the same exact claim.
        if len({source["freshness_class"] for peer, source in distinct.values()}) != 1:
            result["reason"] = "freshness_policy_disagreement"
            return result
        result.update(status="verified", reason="narrow_exact_claim_independent_reviewed_sources",
                      freshness_class=manifest["freshness_class"],
                      support=[{"candidate_id": peer["candidate_id"],
                                "snapshot_id": source["snapshot_id"],
                                "source_id": source["source_id"],
                                "source_url": source["canonical_origin"],
                                "source_sha256": source["content_sha256"],
                                "excerpt": peer["excerpt"], "trust_tier": source["trust_tier"],
                                "retrieved_at": source["retrieved_at"]}
                               for peer, source in list(distinct.values())[:8]])
        return result

    def import_candidate(self, candidate_id: str) -> dict:
        return self._import_candidate(candidate_id, replacing_key=None)

    def _import_candidate(self, candidate_id: str, *, replacing_key: str | None) -> dict:
        decision = self.assess_candidate(candidate_id)
        if decision["status"] != "verified":
            raise ValueError("Bootstrap claim has not passed independent evidence gate: " + decision["reason"])
        key = decision["knowledge_key"]
        store = KnowledgeConsolidationStore(self.root)
        prior = store.lookup(key)
        if prior is not None and (prior["status"] != "active" or prior["freshness"] not in {"fresh", "stale"}):
            raise ValueError("Existing claim lifecycle blocks bootstrap import")
        if prior is not None and prior["freshness_class"] != decision["freshness_class"]:
            raise ValueError("Existing claim freshness policy requires explicit A80 review")
        # Existing opposing verified memory is an unresolved conflict, not a license to overwrite.
        for other in store.search(decision["claim"], limit=12, include_stale=True, include_conflicted=True):
            if other["knowledge_key"] == key:
                continue
            if _opposition(decision["claim"], other["claim_text"]):
                raise ValueError("Contradiction with existing verified memory requires A80 review")
            old_terms = set(re.findall(r"[a-z0-9]+", str(other["claim_text"]).casefold()))
            new_terms = set(re.findall(r"[a-z0-9]+", decision["claim"].casefold()))
            if (len(old_terms & new_terms) >= max(3, int(.8 * min(len(old_terms), len(new_terms))))
                    and other["freshness"] == "fresh" and other["status"] == "active"
                    and other["knowledge_key"] != replacing_key):
                raise ValueError("Related verified claim requires explicit A80 reconciliation")
        new_count = 0
        for support in decision["support"]:
            evidence_id = stable_evidence_id(support["source_sha256"], decision["claim"], support["source_url"])
            args = dict(evidence_id=evidence_id, source_id=support["source_id"],
                        source_url=support["source_url"], confidence=.9,
                        verifier_kind="bootstrap_exact_excerpt_two_host_v1",
                        evidence_sha256=support["source_sha256"],
                        retrieved_at=support["retrieved_at"], verified=True)
            if prior is None:
                new_count += int(store.record_evidence(key, decision["claim"], **args))
            else:
                update = KnowledgeEvolutionStore(self.root).reinforce(
                    key, reason="Independent reviewed bootstrap snapshot", **args)
                new_count += int(update["status"] == "reinforced")
        if prior is None:
            consolidated = store.consolidate(key, stale_after_days=FRESHNESS_POLICIES[decision["freshness_class"]])
            if consolidated.status != "consolidated":
                raise ValueError("Existing knowledge consolidation refused bootstrap claim")
            KnowledgeEvolutionStore(self.root).set_policy(key, decision["freshness_class"],
                                                          reason="Reviewed bootstrap source snapshots")
        result = {"schema": "sira.bootstrap_import.v1", "status": ("imported" if prior is None else
                  "reinforced" if new_count else "duplicate"), "knowledge_key": key,
                  "candidate_id": candidate_id, "new_evidence_count": new_count,
                  "supporting_snapshot_ids": [item["snapshot_id"] for item in decision["support"]],
                  "verification_reason": decision["reason"], "api_requests": 0,
                  "metered_model_requests": 0, "skill_activated": False,
                  "authority_granted": False, "promotion_authorized": False,
                  "paid_spending_authorized": False}
        return result

    def disk_usage(self) -> dict:
        snapshots = self._entries(self.raw, MAX_SNAPSHOTS)
        manifests = self._entries(self.manifests, MAX_SNAPSHOTS)
        candidates = self._entries(self.candidates, MAX_CANDIDATES)
        return {"sources": len(manifests), "raw_bytes": sum(path.stat().st_size for path in snapshots),
                "candidate_records": len(candidates),
                "total_bytes": sum(path.stat().st_size for path in snapshots + manifests + candidates),
                "api_requests": 0, "metered_model_requests": 0}

    def supersede_with_verified_candidate(self, previous_key: str, candidate_id: str) -> dict:
        """Explicit update only; A80 refuses non-current/weak/unrelated replacement."""
        decision = self.assess_candidate(candidate_id)
        if decision["status"] != "verified":
            raise ValueError("New source snapshot is not independently verified")
        if previous_key == decision["knowledge_key"]:
            raise ValueError("Equivalent claim reinforces rather than supersedes")
        old = KnowledgeConsolidationStore(self.root).lookup(previous_key)
        if old is None or old["status"] != "active" or old["freshness"] != "fresh":
            raise ValueError("A current verified predecessor is required")
        # A80 demands objectively stronger, later evidence; preflight before
        # writing new rows so a weak update does not leave two current claims.
        latest = max(item["retrieved_at"] for item in decision["support"])
        if (len({item["source_url"].split("/")[2] for item in decision["support"]}) <= old["host_count"]
                or latest <= old["last_verified_at"]):
            raise ValueError("Newer stronger independent evidence required")
        # Existing A80 policy checks newer, stronger, related evidence, and
        # journals old/new provenance. A failure never removes the old claim.
        imported = self._import_candidate(candidate_id, replacing_key=previous_key)
        event = KnowledgeEvolutionStore(self.root).supersede(
            previous_key, decision["knowledge_key"],
            reason="Newer stronger reviewed bootstrap evidence")
        return {"status": "superseded", "old_key": previous_key,
                "current_key": decision["knowledge_key"], "event_id": event["event_id"],
                "supporting_snapshot_ids": imported["supporting_snapshot_ids"],
                "api_requests": 0, "metered_model_requests": 0, "skill_activated": False}
