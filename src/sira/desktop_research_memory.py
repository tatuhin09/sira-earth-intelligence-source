"""Store verified desktop research *sources* as auditable, low-trust memory.

Independent sources are not proof that every claim in a synthesized answer is
true. Keep the owner's question and the answer in the local research job, not
in the memory summary that can be passed to a chat provider.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .desktop_research import DesktopResearchJobStore, _verification
from .memory import MemoryStore, Observation

_MAX_JOB_BYTES = 2 * 1024 * 1024
_ROUTES = {"free_scholarly_papers", "free_biomedical_mesh", "guarded_gemini_search"}


def ingest_verified_desktop_research(root: Path, job_id: str) -> dict[str, Any]:
    """Index public source titles with a hash of the persisted research job."""
    root = Path(root).resolve()
    path = DesktopResearchJobStore(root).path(job_id)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_JOB_BYTES:
            return {"status": "skipped", "occurrences_added": 0}
        raw = path.read_bytes()
        job = json.loads(raw)
    except (OSError, UnicodeError, ValueError):
        return {"status": "skipped", "occurrences_added": 0}

    if (not isinstance(job, dict)
            or job.get("schema") != "sira.desktop_research_job.v1"
            or job.get("job_id") != job_id
            or job.get("status") != "completed"
            or job.get("route") not in _ROUTES):
        return {"status": "skipped", "occurrences_added": 0}

    sources = job.get("sources")
    verification = job.get("verification")
    if (not isinstance(sources, list) or not 2 <= len(sources) <= 12
            or not isinstance(verification, Mapping)
            or any(not isinstance(source, dict)
                   or not isinstance(source.get("title"), str)
                   or not source["title"].strip() for source in sources)):
        return {"status": "skipped", "occurrences_added": 0}
    expected = _verification(sources, route=str(job["route"]))
    if (expected["status"] not in {"verified_multi_evidence", "verified_multi_source"}
            or any(verification.get(key) != expected[key] for key in (
                "status", "source_count", "independent_evidence_count", "method"
            ))):
        return {"status": "skipped", "occurrences_added": 0}

    titles = [" ".join(source["title"].split())[:240] for source in sources[:2]]
    observation = Observation(
        kind="research",
        category="success",
        capability="desktop_research",
        summary="Research sources: " + "; ".join(titles),
        status="researched",
        origin="research_outcome",
        signature=f"desktop_research_sources:{job_id}",
        details={
            "job_id": job_id,
            "verification_status": expected["status"],
            "independent_evidence_count": expected["independent_evidence_count"],
            "source_ids": [source.get("source_id") for source in sources],
        },
    )
    memory_id, inserted = MemoryStore(root).upsert(
        observation,
        run_id=job_id,
        artifact_name=path.name,
        artifact_sha256=hashlib.sha256(raw).hexdigest(),
        outcome_status="completed",
    )
    return {"status": "learned", "occurrences_added": int(inserted), "memory_id": memory_id}


def reconcile_completed_desktop_research(root: Path) -> dict[str, int]:
    """Replay persisted jobs on Desktop restart; memory occurrence IDs dedupe."""
    learned = errors = 0
    for job in DesktopResearchJobStore(root).list():
        if job.get("status") != "completed":
            continue
        try:
            result = ingest_verified_desktop_research(root, str(job.get("job_id")))
        except (OSError, ValueError, RuntimeError):
            errors += 1
            continue
        learned += result["occurrences_added"]
    return {"learned": learned, "errors": errors}
