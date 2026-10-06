"""Produce an isolated, static competition site from a bounded SIRA snapshot.

This module is run privately. Only PUBLIC_FILES may leave the owner's machine.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from html import escape
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
from typing import Mapping
from zipfile import ZIP_DEFLATED, ZipFile

from .competition_demo import build_public_snapshot
from .desktop_app import DesktopControl

SCHEMA = "sira.competition_static_snapshot.v1"
PUBLIC_FILES = ("index.html", "docs/index.html", "assets/site.css", "snapshot.json", "_headers")
SITE_TEMPLATES = Path(__file__).resolve().parents[2] / "competition_site"
CAPABILITY_SCOPES = {
    "research": ("Research", "Bounded evidence gathering; broad research capability remains evidence scoped."),
    "verified_learning": ("Verified learning", "Narrow claims pass source and corroboration checks."),
    "memory_retrieval": ("Memory retrieval", "Relevant, current verified knowledge can be reused."),
    "knowledge_freshness": ("Knowledge freshness", "Recorded knowledge has freshness and revalidation rules."),
    "planning": ("Planning", "Bounded task planning and prioritization."),
    "coding_engineering": ("Engineering", "Candidate changes are tested in isolated workspaces."),
    "testing": ("Testing", "Deterministic regression and release diagnostics."),
    "verification": ("Verification", "Separate, evidence based verification paths."),
    "tool_use": ("Tool use", "Brokered tool access under existing permissions."),
    "provider_routing": ("Provider routing", "Provider availability and cost policy are observed."),
    "failure_recovery": ("Failure recovery", "Bounded retry, rollback and recovery behavior."),
    "long_horizon_execution": ("Long horizon execution", "Persistent runtime with bounded work cycles."),
    "skill_practice": ("Skill practice", "Practice and independent verification are separate from mastery."),
    "self_improvement": ("Self improvement", "Guarded candidate evaluation and authorization gates."),
}
STATES = {"demonstrated", "partially_demonstrated", "unverified", "degraded", "unavailable"}
RUNTIME_STATES = {"running", "stopped", "unknown"}
HEALTH_STATES = {"ok", "stale_worker", "missing_default", "unknown", "degraded"}
TASK_ROUTES = {"self_status", "verified_memory", "research_needed", "protected_engineering_pipeline", "learning_goal", "research"}
TASK_STATUSES = {"completed", "blocked", "failed", "pending", "running"}
_SENSITIVE_PATTERN = re.compile(
    r"/(?:home|root|workspace|tmp)/|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"
    r"|\b(?:lg|sc|ats|ct|ohf|op|er)_[0-9a-f]{32}\b"
    r"|\bclaim\.[0-9a-f]{32}\b|\bsk-[A-Za-z0-9_-]{12,}\b"
)


class PublicExportError(ValueError):
    """Reject an export rather than publish uncertain or private state."""


def _row(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise PublicExportError("Invalid public count")
    return value


def _enum(value: object, allowed: set[str], default: str = "unknown") -> str:
    return value if isinstance(value, str) and value in allowed else default


def project_public_snapshot(source: Mapping) -> dict:
    """Explicit allowlist. Never serialize a nested private source object."""
    if source.get("schema") != "sira.competition_demo.v1":
        raise PublicExportError("Unexpected source schema")
    release = _row(source.get("release"))
    required = release.get("required_checks")
    passed = release.get("passed_checks")
    if not (release.get("ready") is True and type(required) is int and
            type(passed) is int and 0 < required <= 1000 and passed == required):
        raise PublicExportError("No valid current revision release acceptance")
    authority = _row(source.get("authority"))
    if any(authority.get(key) is not False for key in (
        "runtime_control_exposed", "public_mutation_endpoints", "promotion_authorized",
        "paid_spending_authorized", "skill_activated")):
        raise PublicExportError("Public authority state is not disabled")
    privacy = _row(source.get("privacy"))
    if any(privacy.get(key) is not False for key in (
        "api_keys_exposed", "filesystem_paths_exposed", "source_code_browser_exposed",
        "terminal_or_tool_execution_exposed")):
        raise PublicExportError("Public privacy boundary is not demonstrated")

    runtime = _row(source.get("runtime"))
    knowledge = _row(source.get("knowledge"))
    learning = _row(source.get("learning"))
    providers = _row(source.get("providers"))
    caps = []
    for item in source.get("capabilities", [])[:20]:
        item = _row(item)
        key = item.get("name")
        state = item.get("state")
        if key in CAPABILITY_SCOPES and state in STATES and key not in {c["key"] for c in caps}:
            label, scope = CAPABILITY_SCOPES[key]
            caps.append({"key": key, "label": label, "state": state, "scope": scope})
    research = next((c["state"] for c in caps if c["key"] == "research"), "unverified")
    task = _row(source.get("last_task"))
    route = task.get("route")
    recent = None
    if route in TASK_ROUTES:
        recent = {"route": route,
                  "status": _enum(task.get("status"), TASK_STATUSES),
                  "verification": "not_applicable_operational_status" if route == "self_status"
                  else "evidence_scoped"}
    return {
        "schema": SCHEMA,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "identity": {"name": "SIRA", "expansion": "Self-Improving Research Agent"},
        "runtime": {"state_at_capture": _enum(runtime.get("state"), RUNTIME_STATES),
                    "health_at_capture": _enum(runtime.get("health"), HEALTH_STATES),
                    "generation": _count(runtime.get("generation"))},
        "release": {"ready_at_capture": True, "required_checks": required,
                    "passed_checks": passed},
        "knowledge": {"verified_count": _count(knowledge.get("verified_count")),
                      "conflicted_count": _count(knowledge.get("conflicted_count"))},
        "learning": {"active_goal_count": _count(learning.get("active_goal_count"))},
        "providers": {"known_count": _count(providers.get("known_count")),
                      "available_count": _count(providers.get("available_count")),
                      "cooling_count": _count(providers.get("cooling_count"))},
        "capabilities": caps,
        "research": {"capability_state": research},
        "last_task": recent,
        "public_authority": {"read_only": True, "runtime_control": False,
                             "promotion": False, "paid_spending": False,
                             "skill_activation": False},
    }


def current_git_state(root: Path) -> tuple[str, bool]:
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                          capture_output=True, text=True, timeout=5, check=False)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True, timeout=5, check=False)
    if head.returncode or status.returncode or not re.fullmatch(r"[0-9a-f]{7,40}", head.stdout.strip()):
        raise PublicExportError("Git state unavailable")
    return head.stdout.strip(), not bool(status.stdout.strip())


def _collect_private_values(root: Path) -> tuple[str, ...]:
    """Bounded value based audit; values never appear in reports or exceptions."""
    import getpass
    import pwd
    values = {str(Path.home()), getpass.getuser()}
    gecos = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
    if len(gecos) >= 4:
        values.add(gecos)
    for key, value in os.environ.items():
        if re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", key, re.I) and len(value) >= 8:
            values.add(value)
    env_file = root / ".env"
    if env_file.is_file() and not env_file.is_symlink() and env_file.stat().st_size <= 64_000:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", key, re.I):
                value = value.strip().strip("\"'")
                if len(value) >= 8:
                    values.add(value)
    db = root / "memory/sira_knowledge.sqlite3"
    if db.is_file() and not db.is_symlink():
        connection = sqlite3.connect("file:" + str(db.resolve()) + "?mode=ro", uri=True)
        try:
            for (claim,) in connection.execute("SELECT claim_text FROM consolidated_knowledge LIMIT 256"):
                if len(claim) >= 32:
                    values.add(claim)
        finally:
            connection.close()
    return tuple(value for value in values if len(value) >= 4)


def audit_public_files(files: Mapping[str, bytes], private: tuple[str, ...]) -> None:
    if set(files) != set(PUBLIC_FILES) or sum(len(blob) for blob in files.values()) > 500_000:
        raise PublicExportError("Public file manifest or size invalid")
    for name, blob in files.items():
        if len(blob) > 300_000 or b"\x00" in blob:
            raise PublicExportError("Invalid public file")
        text = blob.decode("utf-8")
        if _SENSITIVE_PATTERN.search(text) or any(value in text for value in private):
            raise PublicExportError("Private value in public export")
        if "127.0.0.1:8765" in text or "127.0.0.1:8877" in text:
            raise PublicExportError("Local service reference in public export")


def _render(snapshot: Mapping) -> dict[str, bytes]:
    source = SITE_TEMPLATES
    templates = (source / "index.html", source / "docs/index.html", source / "assets/site.css")
    if any(p.is_symlink() or not p.is_file() or p.stat().st_size > 180_000 for p in templates):
        raise PublicExportError("Site template unavailable")
    index = templates[0].read_text(encoding="utf-8")
    capability_cards = "".join(
        '<article class="cap"><div class="cap-top"><span>' + escape(row["label"]) +
        '</span><span class="badge ' + escape(row["state"]) + '">' +
        escape(row["state"].replace("_", " ")) + '</span></div><p>' +
        escape(row["scope"]) + '</p></article>' for row in snapshot["capabilities"]
    ) or '<p>No capability evidence is included in this snapshot.</p>'
    values = {
        "{{CAPABILITIES}}": capability_cards,
        "{{CAPTURED_AT}}": escape(snapshot["captured_at"]),
        "{{GENERATION}}": str(snapshot["runtime"]["generation"]),
        "{{RUNTIME}}": escape(snapshot["runtime"]["state_at_capture"]),
        "{{HEALTH}}": escape(snapshot["runtime"]["health_at_capture"]),
        "{{VERIFIED}}": str(snapshot["knowledge"]["verified_count"]),
        "{{GOALS}}": str(snapshot["learning"]["active_goal_count"]),
        "{{CHECKS}}": str(snapshot["release"]["passed_checks"]) + "/" +
                      str(snapshot["release"]["required_checks"]),
        "{{RESEARCH}}": escape(snapshot["research"]["capability_state"].replace("_", " ")),
        "{{PROVIDERS}}": str(snapshot["providers"]["available_count"]) + "/" +
                         str(snapshot["providers"]["known_count"]),
    }
    for marker, value in values.items():
        if marker not in index:
            raise PublicExportError("Site marker missing")
        index = index.replace(marker, value)
    return {
        "index.html": index.encode("utf-8"),
        "docs/index.html": templates[1].read_bytes(),
        "assets/site.css": templates[2].read_bytes(),
        "snapshot.json": (json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False) + "\n").encode("utf-8"),
        "_headers": ("/*\n  Content-Security-Policy: default-src 'none'; style-src 'self'; "
                     "base-uri 'none'; form-action 'none'; frame-ancestors 'none'\n"
                     "  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n"
                     "  X-Frame-Options: DENY\n").encode("utf-8"),
    }


def export_public_site(root: Path, output: Path, *, archive: Path | None = None,
                       private_values: tuple[str, ...] | None = None) -> dict:
    root = Path(root).resolve()
    output = Path(output).absolute()
    if output.exists() or output.is_symlink() or (archive is not None and Path(archive).exists()):
        raise PublicExportError("Export destination exists")
    head, clean = current_git_state(root)
    if not clean:
        raise PublicExportError("Repository must be clean")
    view = DesktopControl(root).overview()
    release = _row(view.get("release"))
    if (release.get("release_ready") is not True or release.get("revision") != head):
        raise PublicExportError("Acceptance is not for current HEAD")
    source = build_public_snapshot(root)
    if (source.get("release", {}).get("required_checks") != release.get("checks_required")
            or source.get("release", {}).get("passed_checks") != release.get("checks_passed")):
        raise PublicExportError("Release changed during capture")
    snapshot = project_public_snapshot(source)
    if current_git_state(root) != (head, True):
        raise PublicExportError("Repository changed during capture")
    files = _render(snapshot)
    audit_public_files(files, private_values if private_values is not None else _collect_private_values(root))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sira-public-stage-", dir=output.parent) as staging:
        stage = Path(staging)
        for name, blob in files.items():
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(blob)
        if current_git_state(root) != (head, True):
            raise PublicExportError("Repository changed before export")
        os.replace(stage, output)
    if archive is not None:
        archive = Path(archive).absolute()
        with ZipFile(archive, "x", ZIP_DEFLATED, compresslevel=9) as bundle:
            for name in PUBLIC_FILES:
                bundle.write(output / name, arcname=name)
    return {"source_revision": head, "release_checks": snapshot["release"],
            "public_files": len(PUBLIC_FILES), "public_bytes": sum(map(len, files.values())),
            "api_requests": 0, "model_requests": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the static, read-only competition site")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(export_public_site(args.root, args.out, archive=args.archive), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
