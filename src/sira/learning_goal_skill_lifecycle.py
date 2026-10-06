"""A76 lifecycle for the one bounded, independently demonstrated practice type.

This is separate from the general skill store. An active entry authorizes no
computer action: it describes only the demonstrated provenance-tracing exercise.
Every read reconciles against fresh A75 decisions and current verified knowledge.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

from .learning_goal_practice_verification import (
    _CAPABILITY, _CANDIDATE_ID, _GOAL_ID, _RUN_ID, _VerificationFailure,
    _current_candidate, _digest, _locked, _read, verify_practice_run,
)
from .models import utc_now
from .storage import write_json


DEFAULT_REQUIRED_SUCCESSES = 2
MAX_REQUIRED_SUCCESSES = 3  # A74 permits at most three runs per candidate.
_VERIFIER_FIELDS = (
    "schema_version", "kind", "verifier_version", "goal_id", "candidate_id",
    "run_id", "expected_capability", "status", "practice_verified",
    "failure_reason", "expected_result", "observed_result", "verifier_evidence",
    "provenance", "reproducibility", "verifier_artifact", "skill_activated",
    "mastery_certified", "lifecycle_evidence_only", "authority_granted",
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _state_path(root: Path, candidate_id: str) -> Path:
    return root / "memory" / "learning_skill_lifecycle" / (candidate_id + ".json")


def _prior(path: Path, goal_id: str, candidate_id: str) -> tuple[dict | None, bool]:
    if not path.exists() and not path.is_symlink():
        return None, False
    try:
        raw, value = _read(path, 64_000)
        if (not isinstance(value, dict) or value.get("goal_id") != goal_id
                or value.get("candidate_id") != candidate_id
                or value.get("schema_version") != 1
                or value.get("kind") != "verified_practice_skill_lifecycle"
                or value.get("record_sha256") != _digest(_canonical({
                    k: v for k, v in value.items() if k != "record_sha256"
                }))):
            return None, True
        return value, False
    except (OSError, ValueError, TypeError, _VerificationFailure):
        return None, True


def _candidate(root: Path, goal_id: str, candidate_id: str) -> dict | None:
    candidate = _current_candidate(root, goal_id, candidate_id)
    if (candidate is None or candidate.get("knowledge_key") is None
            or candidate.get("expected_capability", _CAPABILITY) != _CAPABILITY
            or candidate.get("execution_mode") != "isolated_evidence_trace_v1"):
        return None
    return candidate


def _one_evidence(root: Path, goal_id: str, candidate_id: str,
                  run_id: str, path: Path, candidate: dict,
                  prior_sha256: str | None = None) -> dict | None:
    """Require an existing separate A75 artifact, then independently recheck it."""
    try:
        raw, saved = _read(path, 64_000)
        if prior_sha256 is not None and _digest(raw) != prior_sha256:
            return None
        if (not isinstance(saved, dict) or saved.get("verifier_artifact") != str(path)
                or saved.get("status") != "verified"
                or saved.get("practice_verified") is not True
                or saved.get("skill_activated") is not False
                or saved.get("mastery_certified") is not False
                or saved.get("lifecycle_evidence_only") is not True
                or saved.get("authority_granted") is not False
                or saved.get("goal_id") != goal_id
                or saved.get("candidate_id") != candidate_id
                or saved.get("run_id") != run_id
                or saved.get("verifier_evidence", {}).get("knowledge_key")
                    != candidate["knowledge_key"]
                or saved.get("provenance", {}).get("knowledge_key")
                    != candidate["knowledge_key"]
                or any(saved.get("resource_usage", {}).get(key) != 0 for key in
                       ("api_requests", "metered_model_requests", "paid_requests"))):
            return None
        fresh = verify_practice_run(root, goal_id, candidate_id, run_id)
        if (fresh["status"] != "verified" or fresh["practice_verified"] is not True
                or set(saved) != set(fresh)
                or any(saved.get(key) != fresh.get(key) for key in _VERIFIER_FIELDS)
                or _candidate(root, goal_id, candidate_id) != candidate):
            return None
        new_raw, new = _read(path, 64_000)
        if new != fresh:
            return None
        repro = fresh["reproducibility"]
        run_files = fresh["provenance"]["practice_artifacts"]
        return {
            "run_id": run_id,
            "knowledge_key": candidate["knowledge_key"],
            "claim_sha256": _digest(candidate["claim"].encode("utf-8")),
            "source_urls_sha256": [_digest(url.encode("utf-8"))
                                   for url in candidate["source_urls"]],
            "practice_artifacts": run_files,
            "practice_sha256": {key: repro[key + "_sha256"]
                                for key in ("record", "input", "result")},
            "verification_artifact": str(path),
            "verification_sha256": _digest(new_raw),
            "verification_fingerprint": repro["decision_fingerprint"],
            "independent_host_count": fresh["verifier_evidence"]["independent_host_count"],
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError,
            _VerificationFailure):
        return None


def reconcile_skill_lifecycle(root: Path, goal_id: str, candidate_id: str, *,
                              required_successes: int = DEFAULT_REQUIRED_SUCCESSES) -> dict:
    """Persist a fail-closed state; never infer domain mastery or grant authority."""
    if (not isinstance(goal_id, str) or not _GOAL_ID.fullmatch(goal_id)
            or not isinstance(candidate_id, str) or not _CANDIDATE_ID.fullmatch(candidate_id)
            or type(required_successes) is not int
            or not 2 <= required_successes <= MAX_REQUIRED_SUCCESSES):
        raise ValueError("Invalid lifecycle target or repeated-success policy")
    root = Path(root).resolve()
    memory = root / "memory"
    directory = memory / "learning_skill_lifecycle"
    if memory.is_symlink() or directory.is_symlink():
        raise ValueError("Lifecycle directory is unsafe")
    with _locked(directory):
        path = _state_path(root, candidate_id)
        previous, corrupted = _prior(path, goal_id, candidate_id)
        if (previous is not None and previous.get("required_successes")
                != required_successes):
            raise ValueError("Existing lifecycle policy cannot be silently changed")
        candidate = _candidate(root, goal_id, candidate_id)
        if candidate is None and previous is None and not corrupted:
            raise ValueError("Verified knowledge candidate required")

        previous_active = previous is not None and previous.get("state") == "active"
        old_evidence = {
            row["run_id"]: row for row in (previous.get("evidence") or [])
            if previous_active and isinstance(row, dict)
            and isinstance(row.get("run_id"), str)
        } if previous_active else {}

        evidence: list[dict] = []
        rejected = 0
        verification_dir = memory / "learning_practice_verifications" / candidate_id
        runs_dir = memory / "learning_practice_runs" / candidate_id
        unsafe = any(path.is_symlink() for path in
                     (memory / "learning_practice_verifications", verification_dir,
                      memory / "learning_practice_runs", runs_dir))
        files: list[Path] = []
        if not unsafe and verification_dir.is_dir():
            files = [item for item in verification_dir.iterdir() if item.name != ".lock"]
            if len(files) > 8:
                unsafe = True
        if candidate is not None and not unsafe:
            for item in sorted(files):
                if (not re.fullmatch(r"lpr_[0-9a-f]{32}\.json", item.name)
                        or item.is_symlink()):
                    rejected += 1
                    continue
                run_id = item.stem
                old = old_evidence.get(run_id)
                row = _one_evidence(
                    root, goal_id, candidate_id, run_id, item, candidate,
                    old.get("verification_sha256") if old is not None else None,
                )
                if row is None:
                    rejected += 1
                elif (run_id in {e["run_id"] for e in evidence}
                      or row["verification_fingerprint"] in
                      {e["verification_fingerprint"] for e in evidence}):
                    rejected += 1
                else:
                    evidence.append(row)
        elif unsafe:
            rejected += 1

        prior_evidence = (previous.get("evidence") if previous_active else []) or []
        prior_ids = {row.get("run_id") for row in prior_evidence if isinstance(row, dict)}
        evidence_ids = {row["run_id"] for row in evidence}
        # A missing/tampered supporting run cannot be replaced by a different
        # run without an explicit later recovery checkpoint.
        invalidated = bool(previous_active and
                           (len(prior_ids) < required_successes
                            or not prior_ids.issubset(evidence_ids)))
        if previous is not None and previous.get("state") == "deprecated":
            state, reason = "deprecated", "previous_evidence_invalidated"
        elif corrupted or invalidated or (previous_active and (candidate is None or unsafe)):
            state, reason = "deprecated", "supporting_evidence_invalidated"
        elif candidate is None:
            state, reason = "deprecated", "verified_knowledge_unavailable"
        elif unsafe:
            state, reason = "failed", "evidence_path_unsafe"
        elif rejected:
            # A rejected extra run cannot promote, but does not erase already
            # sound, sufficient support. Its rejection remains auditable.
            state = "active" if len(evidence) >= required_successes else (
                "independently_verified" if evidence else "failed")
            reason = ("repeated_independent_successes" if state == "active" else
                      "more_independent_successes_required" if evidence else
                      "practice_evidence_rejected")
        elif len(evidence) >= required_successes:
            state, reason = "active", "repeated_independent_successes"
        elif evidence:
            state, reason = "independently_verified", "more_independent_successes_required"
        elif runs_dir.is_dir():
            state, reason = "practicing", "independent_verification_required"
        else:
            state, reason = "candidate", "practice_required"

        # A previously active record is itself an integrity boundary. Verify
        # the exact supporting verifier bytes before accepting fresh A75 writes.
        if previous_active and state == "active":
            old = {row["run_id"]: row for row in prior_evidence}
            if any(not any(row["run_id"] == run_id and
                           row["verification_fingerprint"] == prior["verification_fingerprint"]
                           for row in evidence)
                   for run_id, prior in old.items()):
                state, reason = "deprecated", "supporting_evidence_invalidated"

        record = {
            "schema_version": 1, "kind": "verified_practice_skill_lifecycle",
            "goal_id": goal_id, "candidate_id": candidate_id,
            "knowledge_key": candidate.get("knowledge_key") if candidate else
                (previous.get("knowledge_key") if previous else None),
            "scope": _CAPABILITY, "state": state, "active": state == "active",
            "reason": reason, "required_successes": required_successes,
            "success_count": len(evidence), "rejected_count": rejected,
            "evidence_strength": ("two_distinct_independent_runs" if state == "active"
                                  and required_successes == 2 else
                                  "three_distinct_independent_runs" if state == "active"
                                  else "insufficient_independent_runs"),
            "evidence": evidence,
            "domain_mastery_certified": False,
            "authority_granted": False, "promotion_performed": False,
            "paid_spending": False, "updated_at": utc_now(),
            "artifact": str(path),
        }
        record["record_sha256"] = _digest(_canonical(record))
        if path.is_symlink():
            raise ValueError("Lifecycle artifact is unsafe")
        write_json(path, record)
        return record


def get_skill_lifecycle(root: Path, goal_id: str, candidate_id: str) -> dict:
    """Revalidate all support on each read; stale active files never grant status."""
    root = Path(root).resolve()
    path = _state_path(root, candidate_id)
    previous, corrupted = _prior(path, goal_id, candidate_id)
    threshold = previous.get("required_successes") if previous else DEFAULT_REQUIRED_SUCCESSES
    if corrupted:
        threshold = DEFAULT_REQUIRED_SUCCESSES
    return reconcile_skill_lifecycle(root, goal_id, candidate_id,
                                     required_successes=threshold)


def list_active_practice_skills(root: Path, goal_id: str) -> list[dict]:
    """Only expose live, freshly reconciled bounded practice capabilities."""
    if not isinstance(goal_id, str) or not _GOAL_ID.fullmatch(goal_id):
        raise ValueError("Invalid goal identifier")
    root = Path(root).resolve()
    directory = root / "memory" / "learning_skill_lifecycle"
    if directory.is_symlink() or not directory.is_dir():
        return []
    files = list(directory.glob("lsc_*.json"))
    if len(files) > 128:
        return []
    active = []
    for path in sorted(files):
        if not re.fullmatch(r"lsc_[0-9a-f]{24}\.json", path.name):
            continue
        try:
            saved, _ = _prior(path, goal_id, path.stem)
            if saved is None or saved.get("goal_id") != goal_id:
                continue
            current = get_skill_lifecycle(root, goal_id, path.stem)
            if current["active"]:
                active.append(current)
        except (OSError, ValueError, TypeError):
            continue
    return active


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile bounded verified practice")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--required-successes", type=int, default=DEFAULT_REQUIRED_SUCCESSES)
    parser.add_argument("goal_id")
    parser.add_argument("candidate_id")
    args = parser.parse_args(argv)
    state = reconcile_skill_lifecycle(args.root, args.goal_id, args.candidate_id,
                                      required_successes=args.required_successes)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0 if state["state"] in {"candidate", "practicing",
                                    "independently_verified", "active"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
