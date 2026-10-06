"""Independent non-authoritative evaluator for v1.8 engineering candidates.

This gate re-checks a completed v1.8E writer attempt without widening SIRA's
existing Evaluator1/Evaluator2/Promotion authority. Passing means only that the
candidate is eligible to be considered by a future protected engineering gate.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .engineering_editing import (
    COPY_MANIFESTS,
    IGNORED_COPY_DIRS,
    MAX_EDIT_FILE_BYTES,
    MAX_EDIT_FILES,
    MAX_EDIT_TOTAL_BYTES,
    VERIFICATION_SUPPORT_SUFFIXES,
    EngineeringCandidateEditError,
    _copy_digest,
    _iter_copyable_files,
    _validate_edit_relative_path,
)
from .models import utc_now
from .advanced_evaluation import evaluate_candidate, suites_for_target
from .anti_gaming import (
    analyze_change_integrity,
    evaluation_registry_digest,
    finalize_firewall,
)

ENGINEERING_EVALUATOR_POLICY_VERSION = 1
MAX_UNEXPECTED_PATHS = 40

_SENSITIVE_EXACT_NAMES = frozenset({
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.test",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "service-account.json",
})
_SENSITIVE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_sensitive_name(name: str) -> bool:
    lower = name.casefold()
    return (
        lower in _SENSITIVE_EXACT_NAMES
        or lower.endswith(_SENSITIVE_SUFFIXES)
    )


def _is_utf8_text(payload: bytes) -> bool:
    if b"\x00" in payload:
        return False
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _safe_candidate_root(
    main_root: Path,
    candidate_root: Path,
) -> Path:
    main_root = Path(main_root).resolve()
    raw = Path(candidate_root)
    if raw.is_symlink():
        raise ValueError("engineering candidate root must not be a symlink")
    candidate = raw.resolve()
    if not main_root.is_dir() or not candidate.is_dir():
        raise ValueError("engineering main and candidate roots must exist")
    if candidate == main_root:
        raise ValueError("engineering candidate cannot be the main project")
    try:
        candidate.relative_to(main_root)
    except ValueError:
        return candidate
    raise ValueError("engineering candidate must be outside the main project")


def _surface_anomalies(
    candidate_root: Path,
) -> dict[str, list[str]]:
    """Find non-generated surface artifacts without following symlinks."""
    candidate_root = Path(candidate_root).resolve()
    symlinks: list[str] = []
    sensitive: list[str] = []
    unexpected: list[str] = []

    for current, dirs, names in os.walk(
        candidate_root,
        followlinks=False,
    ):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            child = current_path / name
            relative = child.relative_to(candidate_root).as_posix()
            if child.is_symlink():
                symlinks.append(relative)
                continue
            if name in IGNORED_COPY_DIRS or name.startswith("."):
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs

        for name in sorted(names):
            child = current_path / name
            relative = child.relative_to(candidate_root).as_posix()
            if child.is_symlink():
                symlinks.append(relative)
                continue
            if not child.is_file():
                unexpected.append(relative)
                continue
            if _is_sensitive_name(name):
                sensitive.append(relative)
                continue

            rel = PurePosixPath(relative)
            if len(rel.parts) == 1 and name in COPY_MANIFESTS:
                continue
            suffix = rel.suffix.casefold()
            # Support assets are copied for tests; the full-tree diff below
            # still requires their bytes to match the source baseline.
            if suffix in VERIFICATION_SUPPORT_SUFFIXES:
                continue
            if suffix in {
                ".py",
                ".js", ".jsx", ".mjs", ".cjs",
                ".ts", ".tsx",
                ".dart",
                ".java",
                ".c", ".h",
                ".cc", ".cpp", ".cxx", ".hh", ".hpp",
                ".rs",
                ".go",
                ".sql",
            }:
                continue
            if name.startswith("."):
                continue
            unexpected.append(relative)

    return {
        "symlink_paths": sorted(symlinks)[:MAX_UNEXPECTED_PATHS],
        "sensitive_paths": sorted(sensitive)[:MAX_UNEXPECTED_PATHS],
        "unexpected_paths": sorted(unexpected)[:MAX_UNEXPECTED_PATHS],
    }


def _snapshot_copyable_tree(
    root: Path,
) -> dict[str, dict[str, object]]:
    files: dict[str, dict[str, object]] = {}
    for path, relative, size in _iter_copyable_files(root):
        payload = path.read_bytes()
        files[relative.as_posix()] = {
            "size": size,
            "sha256": _sha256(payload),
            "utf8": _is_utf8_text(payload),
        }
    return files


def _verification_clean(
    verification: Mapping[str, object] | None,
    attempt: Mapping[str, object],
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if not isinstance(verification, Mapping):
        return False, ["verification_missing"]

    if (
        verification.get("schema")
        != "sira.engineering_verification_result.v1"
    ):
        failures.append("verification_schema_invalid")
    if verification.get("overall_passed") is not True:
        failures.append("verification_not_passed")
    if verification.get("status") != "completed":
        failures.append("verification_not_completed")
    if verification.get("outcome") != "verification_passed":
        failures.append("verification_outcome_invalid")
    if verification.get("authority_granted") is not False:
        failures.append("verification_claimed_authority")
    if verification.get("promotion_authorized") is not False:
        failures.append("verification_claimed_promotion")
    if verification.get("raw_output_included") is not False:
        failures.append("raw_output_contract_invalid")
    if attempt.get("verification_executed") is not True:
        failures.append("verification_not_executed")

    executed = verification.get("processes_executed")
    if type(executed) is not int or executed < 1:
        failures.append("verification_process_count_invalid")

    missing = verification.get("missing_command_ids")
    if not isinstance(missing, list) or missing:
        failures.append("verification_commands_missing")

    diagnostic_count = verification.get("diagnostic_count")
    if type(diagnostic_count) is not int or diagnostic_count != 0:
        failures.append("verification_diagnostics_not_clean")

    commands = verification.get("commands")
    if not isinstance(commands, list) or not commands:
        failures.append("verification_commands_invalid")
    else:
        for row in commands:
            if (
                not isinstance(row, Mapping)
                or row.get("status") != "passed"
                or row.get("returncode") != 0
                or row.get("timed_out") is not False
                or row.get("output_limit_exceeded") is not False
            ):
                failures.append("verification_command_failed")
                break

    return not failures, sorted(set(failures))


def evaluate_engineering_candidate(
    main_root: Path,
    writer_attempt: Mapping[str, object],
    *,
    target_evidence: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Independently assess one v1.8E writer attempt.

    The result is advisory. It never authorizes or performs promotion.
    """
    main_root = Path(main_root).resolve()
    if not main_root.is_dir():
        raise ValueError("engineering main root must exist")
    if not isinstance(writer_attempt, Mapping):
        raise ValueError("engineering writer attempt must be a mapping")

    risk_flags: set[str] = set()
    checks: dict[str, bool] = {}

    attempt_schema_ok = (
        writer_attempt.get("schema")
        == "sira.engineering_writer_attempt.v1"
    )
    attempt_status_ok = (
        writer_attempt.get("status")
        == "verified_candidate_ready"
    )
    checks["attempt_schema_valid"] = attempt_schema_ok
    checks["attempt_verified_status"] = attempt_status_ok

    candidate_value = writer_attempt.get("candidate_root")
    if not isinstance(candidate_value, str) or not candidate_value:
        candidate = None
        risk_flags.add("candidate_root_missing")
    else:
        try:
            candidate = _safe_candidate_root(
                main_root,
                Path(candidate_value),
            )
        except ValueError:
            candidate = None
            risk_flags.add("candidate_root_unsafe")

    edit_candidate = writer_attempt.get("edit_candidate")
    edit_candidate_ok = bool(
        isinstance(edit_candidate, Mapping)
        and edit_candidate.get("schema")
        == "sira.engineering_edit_candidate.v1"
        and edit_candidate.get("status")
        == "candidate_prepared"
        and edit_candidate.get("source_root")
        == str(main_root)
        and candidate is not None
        and edit_candidate.get("candidate_root")
        == str(candidate)
        and edit_candidate.get("main_tree_modified") is False
        and edit_candidate.get("package_installation_performed") is False
        and edit_candidate.get("promotion_authorized") is False
    )
    checks["edit_candidate_valid"] = edit_candidate_ok

    manifest = (
        edit_candidate.get("candidate_manifest")
        if isinstance(edit_candidate, Mapping)
        else None
    )
    manifest_ok = bool(
        isinstance(manifest, Mapping)
        and manifest.get("schema")
        == "sira.engineering_candidate_manifest.v1"
        and manifest.get("source_root")
        == str(main_root)
        and candidate is not None
        and manifest.get("candidate_root")
        == str(candidate)
        and isinstance(manifest.get("source_copy_sha256"), str)
        and len(str(manifest.get("source_copy_sha256"))) == 64
        and manifest.get("symlinks_copied") is False
        and manifest.get("sensitive_artifact_patterns_excluded") is True
        and manifest.get("vendor_dependency_trees_excluded") is True
        and manifest.get("main_tree_modified") is False
        and manifest.get("package_installation_performed") is False
        and manifest.get("promotion_allowed") is False
        and manifest.get("authority_granted") is False
    )
    checks["candidate_manifest_valid"] = manifest_ok

    edit_report = (
        edit_candidate.get("edit_report")
        if isinstance(edit_candidate, Mapping)
        else None
    )
    edit_report_ok = bool(
        isinstance(edit_report, Mapping)
        and edit_report.get("schema")
        == "sira.engineering_edit_result.v1"
        and edit_report.get("candidate_only") is True
        and edit_report.get("verification_required") is True
        and edit_report.get("main_tree_modified") is False
        and edit_report.get("package_installation_performed") is False
        and edit_report.get("authority_granted") is False
        and edit_report.get("promotion_performed") is False
        and edit_report.get("promotion_authorized") is False
    )
    checks["edit_report_valid"] = edit_report_ok

    writer_report = writer_attempt.get("writer_report")
    writer_report_ok = bool(
        isinstance(writer_report, Mapping)
        and writer_report.get("schema")
        == "sira.engineering_writer_report.v1"
        and writer_report.get("candidate_only") is True
        and writer_report.get("main_tree_modified") is False
        and writer_report.get("promotion_authorized") is False
    )
    checks["writer_report_valid"] = writer_report_ok

    if not (
        attempt_schema_ok
        and attempt_status_ok
        and edit_candidate_ok
        and manifest_ok
        and edit_report_ok
        and writer_report_ok
        and candidate is not None
    ):
        risk_flags.add("attempt_integrity_failed")

    current_source_digest: str | None = None
    base_current = False
    if manifest_ok:
        try:
            current_source_digest, _, _ = _copy_digest(main_root)
            base_current = (
                current_source_digest
                == manifest.get("source_copy_sha256")
            )
        except (OSError, EngineeringCandidateEditError):
            risk_flags.add("main_snapshot_failed")
    checks["candidate_base_current"] = base_current
    if manifest_ok and not base_current:
        risk_flags.add("stale_candidate_base")

    expected_changed: list[str] = []
    expected_hashes: dict[str, str] = {}
    policy = (
        edit_candidate.get("policy")
        if isinstance(edit_candidate, Mapping)
        else None
    )
    if edit_report_ok and isinstance(policy, Mapping):
        raw_changed = edit_report.get("files_changed")
        raw_hashes = edit_report.get("file_sha256")
        if (
            isinstance(raw_changed, list)
            and raw_changed
            and len(raw_changed) <= MAX_EDIT_FILES
            and len(set(raw_changed)) == len(raw_changed)
            and isinstance(raw_hashes, Mapping)
        ):
            try:
                for relative in raw_changed:
                    if not isinstance(relative, str):
                        raise EngineeringCandidateEditError(
                            "invalid changed path"
                        )
                    normalized, _ = _validate_edit_relative_path(
                        relative,
                        policy,
                    )
                    digest = raw_hashes.get(normalized)
                    if (
                        not isinstance(digest, str)
                        or len(digest) != 64
                    ):
                        raise EngineeringCandidateEditError(
                            "missing edit digest"
                        )
                    expected_changed.append(normalized)
                    expected_hashes[normalized] = digest
            except EngineeringCandidateEditError:
                risk_flags.add("declared_edit_invalid")
        else:
            risk_flags.add("declared_edit_invalid")
    else:
        risk_flags.add("declared_edit_invalid")

    expected_changed = sorted(set(expected_changed))
    checks["declared_edits_valid"] = bool(
        expected_changed
        and "declared_edit_invalid" not in risk_flags
    )

    main_files: dict[str, dict[str, object]] = {}
    candidate_files: dict[str, dict[str, object]] = {}
    anomalies = {
        "symlink_paths": [],
        "sensitive_paths": [],
        "unexpected_paths": [],
    }
    added: list[str] = []
    removed: list[str] = []
    modified: list[str] = []
    actual_changed: list[str] = []
    changed_bytes = 0

    if candidate is not None:
        try:
            main_files = _snapshot_copyable_tree(main_root)
            candidate_files = _snapshot_copyable_tree(candidate)
            anomalies = _surface_anomalies(candidate)

            main_paths = set(main_files)
            candidate_paths = set(candidate_files)
            added = sorted(candidate_paths - main_paths)
            removed = sorted(main_paths - candidate_paths)
            modified = sorted(
                relative
                for relative in (main_paths & candidate_paths)
                if (
                    main_files[relative]["sha256"]
                    != candidate_files[relative]["sha256"]
                )
            )
            actual_changed = sorted(set(added) | set(modified) | set(removed))
            changed_regular = [
                relative
                for relative in sorted(set(added) | set(modified))
                if relative in candidate_files
            ]
            changed_bytes = sum(
                int(candidate_files[relative]["size"])
                for relative in changed_regular
            )
        except (OSError, EngineeringCandidateEditError):
            risk_flags.add("candidate_snapshot_failed")

    if anomalies["symlink_paths"]:
        risk_flags.add("symlink_present")
    if anomalies["sensitive_paths"]:
        risk_flags.add("sensitive_artifact_present")
    if anomalies["unexpected_paths"]:
        risk_flags.add("unexpected_candidate_file")
    if removed:
        risk_flags.add("file_removed")
    if actual_changed != expected_changed:
        risk_flags.add("changed_set_mismatch")
    if len(actual_changed) > MAX_EDIT_FILES:
        risk_flags.add("too_many_changed_files")
    if changed_bytes > MAX_EDIT_TOTAL_BYTES:
        risk_flags.add("oversized_change_batch")

    candidate_hashes_match = bool(expected_changed)
    text_only = True
    per_file_bounded = True
    for relative in expected_changed:
        metadata = candidate_files.get(relative)
        if not isinstance(metadata, Mapping):
            candidate_hashes_match = False
            text_only = False
            per_file_bounded = False
            continue
        if metadata.get("sha256") != expected_hashes.get(relative):
            candidate_hashes_match = False
        if metadata.get("utf8") is not True:
            text_only = False
        size = metadata.get("size")
        if type(size) is not int or size > MAX_EDIT_FILE_BYTES:
            per_file_bounded = False

    if not candidate_hashes_match:
        risk_flags.add("candidate_hash_mismatch")
    if not text_only:
        risk_flags.add("non_utf8_change")
    if not per_file_bounded:
        risk_flags.add("oversized_changed_file")

    checks["changed_set_matches_declaration"] = (
        bool(expected_changed)
        and actual_changed == expected_changed
    )
    checks["candidate_hashes_match_edit_report"] = candidate_hashes_match
    checks["text_only"] = text_only
    checks["bounded_diff"] = (
        per_file_bounded
        and len(actual_changed) <= MAX_EDIT_FILES
        and changed_bytes <= MAX_EDIT_TOTAL_BYTES
    )
    checks["symlink_free"] = not anomalies["symlink_paths"]
    checks["sensitive_artifacts_absent"] = not anomalies["sensitive_paths"]
    checks["unexpected_files_absent"] = not anomalies["unexpected_paths"]
    checks["no_file_removals"] = not removed

    verification = writer_attempt.get("verification")
    verification_ok, verification_failures = _verification_clean(
        verification if isinstance(verification, Mapping) else None,
        writer_attempt,
    )
    checks["verification_clean"] = verification_ok
    risk_flags.update(verification_failures)

    advisory = writer_attempt.get("diagnostic_advisory")
    advisory_ok = bool(
        isinstance(advisory, Mapping)
        and advisory.get("schema")
        == "sira.engineering_diagnostic_advisory.v1"
        and advisory.get("diagnostic_count") == 0
        and advisory.get("repair_authority") == "advisory_only"
        and advisory.get("raw_output_included") is False
        and advisory.get("authority_granted") is False
        and advisory.get("promotion_authorized") is False
    )
    checks["diagnostic_advisory_clean"] = advisory_ok
    if not advisory_ok:
        risk_flags.add("diagnostic_advisory_not_clean")

    structural_flags = {
        "attempt_integrity_failed",
        "candidate_root_missing",
        "candidate_root_unsafe",
        "declared_edit_invalid",
        "candidate_snapshot_failed",
        "symlink_present",
        "sensitive_artifact_present",
        "unexpected_candidate_file",
        "file_removed",
        "changed_set_mismatch",
        "candidate_hash_mismatch",
        "non_utf8_change",
        "too_many_changed_files",
        "oversized_changed_file",
        "oversized_change_batch",
    }
    structural_ok = not bool(risk_flags & structural_flags)
    checks["structural_integrity"] = structural_ok

    # A86: change-integrity and anti-gaming analysis runs before A85 so that a
    # candidate with blocking evidence is never granted independent execution.
    registry_before: str | None = None
    try:
        registry_before = evaluation_registry_digest(main_root)
    except (OSError, ValueError, TypeError, KeyError):
        registry_before = None  # finalize_firewall records it as unverifiable
    firewall_analysis = None
    if structural_ok and candidate is not None:
        try:
            firewall_analysis = analyze_change_integrity(
                main_root, candidate,
                added=added, modified=modified, removed=removed,
            )
        except Exception:  # noqa: BLE001 - fail closed, never crash the gate
            risk_flags.add("anti_gaming_analysis_failed")
    firewall_blocking = bool(
        firewall_analysis
        and any(f["severity"] == "blocking" for f in firewall_analysis["flags"])
    )

    advanced_reports = []
    if (structural_ok and verification_ok and advisory_ok and attempt_status_ok
            and candidate is not None and not firewall_blocking):
        try:
            suite_ids = sorted(set(suite for path in expected_changed
                                   for suite in suites_for_target(main_root, path)))
            if len(suite_ids) > 4:
                raise ValueError("too many applicable evaluation suites")
            for suite_id in suite_ids:
                advanced_reports.append(evaluate_candidate(
                    main_root, main_root, candidate, suite_id,
                    seed=secrets.randbits(63),
                    baseline_id=current_source_digest,
                    candidate_id=_sha256(str(expected_hashes).encode("utf-8")),
                ))
            if any(report["verdict"] != "pass" for report in advanced_reports):
                risk_flags.add("independent_evaluation_not_passed")
        except (OSError, ValueError, TypeError):
            risk_flags.add("independent_evaluation_integrity_failed")
    checks["independent_evaluation"] = not bool(risk_flags & {
        "independent_evaluation_not_passed", "independent_evaluation_integrity_failed"})

    firewall = None
    if firewall_analysis is not None:
        try:
            firewall = finalize_firewall(
                main_root, firewall_analysis,
                a85_reports=advanced_reports,
                registry_before=registry_before,
                baseline_id=current_source_digest,
                candidate_id=_sha256(str(expected_hashes).encode("utf-8")),
                verification_clean=bool(verification_ok and advisory_ok),
                target_evidence=target_evidence,
            )
        except Exception:  # noqa: BLE001 - fail closed, never crash the gate
            risk_flags.add("anti_gaming_firewall_error")
    checks["anti_gaming_firewall"] = bool(
        firewall is not None and firewall["verdict"] == "eligible"
    )
    if firewall is not None and firewall["verdict"] != "eligible":
        risk_flags.add("anti_gaming_" + str(firewall["verdict"]))

    if "stale_candidate_base" in risk_flags:
        decision, code = "block", "stale_candidate_base"
    elif not (
        attempt_schema_ok
        and edit_candidate_ok
        and manifest_ok
        and edit_report_ok
        and writer_report_ok
    ):
        decision, code = "reject", "attempt_integrity_failed"
    elif not structural_ok:
        decision, code = "reject", "candidate_integrity_failed"
    elif not verification_ok or not advisory_ok or not attempt_status_ok:
        decision, code = "reject", "verification_not_clean"
    elif not actual_changed:
        decision, code = "reject", "no_effective_change"
    elif not checks["independent_evaluation"]:
        decision, code = "reject", "independent_evaluation_not_passed"
    elif firewall is not None and firewall["verdict"] == "blocked":
        decision, code = "reject", "anti_gaming_blocked"
    elif not checks["anti_gaming_firewall"]:
        decision, code = "block", "integrity_inconclusive"
    else:
        decision, code = "accept", "engineering_candidate_verified"

    eligible = (
        decision == "accept"
        and code == "engineering_candidate_verified"
    )

    return {
        "schema": "sira.engineering_evaluator_gate.v1",
        "policy_version": ENGINEERING_EVALUATOR_POLICY_VERSION,
        "created_at": utc_now(),
        "decision": decision,
        "decision_code": code,
        "recommendation": (
            "eligible_for_protected_gate"
            if eligible
            else "do_not_promote"
        ),
        "promotion_candidate_eligible": eligible,
        "promotion_authorized": False,
        "promotion_performed": False,
        "authority_granted": False,
        "main_tree_modified": False,
        "checks": checks,
        "independent_evaluation": [{"evaluation_id": r["evaluation_id"],
                                    "suite_id": r["suite_id"], "verdict": r["verdict"],
                                    "suite_sha256": r["suite_sha256"]}
                                   for r in advanced_reports],
        "anti_gaming": (
            None if firewall is None else {
                "firewall_id": firewall["firewall_id"],
                "verdict": firewall["verdict"],
                "reason": firewall["reason"],
                "artifact": firewall.get("artifact"),
                "flags": [{"code": f["code"], "severity": f["severity"],
                           "path": f["path"]} for f in firewall["flags"]],
                "regression_floors": {x["floor"]: x["status"]
                                      for x in firewall["regression_floors"]},
                "target_improvement": firewall["target_improvement"]["status"],
                "independent_evaluation_present":
                    firewall["independent_evaluation"]["a85_present"],
                "promotion_authorized": False,
            }),
        "risk_flags": sorted(risk_flags),
        "source_binding": {
            "manifest_source_copy_sha256": (
                manifest.get("source_copy_sha256")
                if isinstance(manifest, Mapping)
                else None
            ),
            "current_source_copy_sha256": current_source_digest,
            "candidate_base_current": base_current,
        },
        "diff": {
            "declared_changed_files": expected_changed,
            "added_files": added,
            "modified_files": modified,
            "removed_files": removed,
            "actual_changed_files": actual_changed,
            "changed_file_count": len(actual_changed),
            "changed_bytes": changed_bytes,
            "symlink_paths": anomalies["symlink_paths"],
            "sensitive_paths": anomalies["sensitive_paths"],
            "unexpected_paths": anomalies["unexpected_paths"],
        },
        "verification": {
            "overall_passed": (
                verification.get("overall_passed")
                if isinstance(verification, Mapping)
                else None
            ),
            "status": (
                verification.get("status")
                if isinstance(verification, Mapping)
                else None
            ),
            "outcome": (
                verification.get("outcome")
                if isinstance(verification, Mapping)
                else None
            ),
            "processes_executed": (
                verification.get("processes_executed")
                if isinstance(verification, Mapping)
                else None
            ),
            "diagnostic_count": (
                verification.get("diagnostic_count")
                if isinstance(verification, Mapping)
                else None
            ),
            "failures": verification_failures,
        },
    }
