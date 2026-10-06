"""Protected explicit research boundary for SIRA Desktop v0.4.

Research is never triggered by ordinary conversation. Explicit research uses
free/public scholarly or biomedical sources first. General web fallback uses
Gemini Search grounding only after a separate owner zero-cost confirmation.
This module has no runtime-control, promotion, payment, package-install, or
protected-code authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from .desktop_chat import (
    DesktopChatSettingsStore,
    run_desktop_model_chat,
)
from .gemini_research_runtime import run_gemini_research
from .models import utc_now
from .providers.gemini_research import MODE_GOOGLE_SEARCH
from .research_mesh import (
    research_mesh_plan,
    search_biomedical_free,
)
from .storage import write_json

POLICY_VERSION = 1
MAX_QUERY_CHARS = 2000
MAX_PAPERS = 3
MAX_SOURCES = 12
MAX_SOURCE_TEXT = 900
MAX_JOB_HISTORY = 30
SEARCH_CONFIRM_ENV = "SIRA_GEMINI_SEARCH_ZERO_COST_CONFIRMED"

_EXPLICIT_PATTERNS = (
    re.compile(r"^\s*research\b", re.I),
    re.compile(r"^\s*verify\b", re.I),
    re.compile(r"^\s*fact[- ]?check\b", re.I),
    re.compile(r"^\s*find\s+(papers?|evidence)\b", re.I),
    re.compile(r"(?:রিসার্চ|গবেষণা)\s*(?:কর|করো|করে|করি)", re.I),
    re.compile(r"(?:যাচাই|ভেরিফাই)\s*(?:কর|করো|করে)", re.I),
    re.compile(r"(?:পেপার|প্রমাণ|evidence)\s+(?:খুঁজ|খোজ)", re.I),
)

_BIOMEDICAL_HINTS = (
    "medical", "medicine", "clinical", "disease", "patient",
    "health", "drug", "therapy", "cancer", "diabetes",
    "biomedical", "pubmed", "রোগ", "চিকিৎসা", "স্বাস্থ্য",
    "ওষুধ", "রোগী",
)
_SCHOLARLY_HINTS = (
    "paper", "papers", "study", "studies", "journal",
    "academic", "scholarly", "research paper", "literature",
    "arxiv", "doi", "পেপার", "গবেষণাপত্র", "জার্নাল",
)


def explicit_research_requested(message: str) -> bool:
    if not isinstance(message, str):
        return False
    return any(pattern.search(message) for pattern in _EXPLICIT_PATTERNS)


def _clean_query(message: str) -> str:
    if not isinstance(message, str):
        raise ValueError("research query must be text")
    query = " ".join(message.split())
    if not 1 <= len(query) <= MAX_QUERY_CHARS:
        raise ValueError(
            f"research query must contain 1..{MAX_QUERY_CHARS} characters"
        )
    query.encode("utf-8")
    return query


def classify_research_domain(query: str) -> str:
    lowered = query.casefold()
    if any(token in lowered for token in _BIOMEDICAL_HINTS):
        return "biomedical"
    if any(token in lowered for token in _SCHOLARLY_HINTS):
        return "scholarly"
    return "general_web"


class DesktopResearchSettingsStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.path = (
            self.root
            / "runtime"
            / "desktop"
            / "research_settings.json"
        )

    def read(self) -> dict[str, object]:
        default = {
            "schema": "sira.desktop_research_settings.v1",
            "gemini_search_zero_cost_confirmed": False,
        }
        try:
            if (
                self.path.is_symlink()
                or not self.path.is_file()
                or self.path.stat().st_size > 16 * 1024
            ):
                return default
            value = json.loads(
                self.path.read_text(encoding="utf-8")
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            return default
        if (
            not isinstance(value, dict)
            or value.get("schema")
            != "sira.desktop_research_settings.v1"
            or type(
                value.get(
                    "gemini_search_zero_cost_confirmed"
                )
            )
            is not bool
        ):
            return default
        return {
            "schema": "sira.desktop_research_settings.v1",
            "gemini_search_zero_cost_confirmed": bool(
                value[
                    "gemini_search_zero_cost_confirmed"
                ]
            ),
        }

    def set_search_zero_cost_confirmed(
        self,
        confirmed: bool,
    ) -> dict[str, object]:
        if type(confirmed) is not bool:
            raise ValueError(
                "search zero-cost confirmation must be boolean"
            )
        payload = {
            "schema": "sira.desktop_research_settings.v1",
            "gemini_search_zero_cost_confirmed": confirmed,
            "updated_at": utc_now(),
            "paid_spending_authorized": False,
            "billing_changes_authorized": False,
        }
        write_json(self.path, payload)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return payload


class DesktopResearchJobStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.directory = (
            self.root
            / "runtime"
            / "desktop"
            / "research_jobs"
        )

    def path(self, job_id: str) -> Path:
        if (
            not isinstance(job_id, str)
            or not re.fullmatch(
                r"dr_[0-9a-f]{32}",
                job_id,
            )
        ):
            raise ValueError("invalid desktop research job id")
        return self.directory / f"{job_id}.json"

    def create(
        self,
        query: str,
        domain: str,
    ) -> dict[str, object]:
        job_id = "dr_" + uuid4().hex
        payload = {
            "schema": "sira.desktop_research_job.v1",
            "policy_version": POLICY_VERSION,
            "job_id": job_id,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "query": query,
            "domain": domain,
            "status": "queued",
            "progress": "queued",
            "progress_percent": 0,
            "route": None,
            "answer": None,
            "sources": [],
            "verification": {
                "status": "pending",
                "source_count": 0,
                "independent_evidence_count": 0,
            },
            "metrics": {
                "api_requests": 0,
                "metered_model_requests": 0,
                "paid_requests": 0,
            },
            "paid_spending": False,
            "authority_granted": False,
            "promotion_performed": False,
            "runtime_control_performed": False,
        }
        self.save(payload)
        return payload

    def save(
        self,
        payload: Mapping[str, object],
    ) -> None:
        job_id = payload.get("job_id")
        path = self.path(str(job_id))
        value = dict(payload)
        value["updated_at"] = utc_now()
        write_json(path, value)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def read(
        self,
        job_id: str,
    ) -> dict[str, object] | None:
        path = self.path(job_id)
        try:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > 2 * 1024 * 1024
            ):
                return None
            value = json.loads(
                path.read_text(encoding="utf-8")
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            return None
        return value if isinstance(value, dict) else None

    def list(self) -> list[dict[str, object]]:
        try:
            paths = [
                path
                for path in self.directory.glob("dr_*.json")
                if path.is_file()
                and not path.is_symlink()
            ]
            paths.sort(
                key=lambda path: (
                    path.stat().st_mtime_ns,
                    path.name,
                ),
                reverse=True,
            )
        except OSError:
            return []
        rows: list[dict[str, object]] = []
        for path in paths[:MAX_JOB_HISTORY]:
            try:
                value = json.loads(
                    path.read_text(encoding="utf-8")
                )
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
            ):
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows


def _update(
    store: DesktopResearchJobStore,
    job: dict[str, object],
    *,
    status: str | None = None,
    progress: str | None = None,
    progress_percent: int | None = None,
    **fields: object,
) -> None:
    if status is not None:
        job["status"] = status
    if progress is not None:
        job["progress"] = progress
    if progress_percent is not None:
        job["progress_percent"] = max(
            0,
            min(100, int(progress_percent)),
        )
    job.update(fields)
    store.save(job)


def _parse_cli_json(stdout: str) -> dict[str, object]:
    text = stdout.strip()
    if not text:
        raise ValueError("papers command returned empty output")
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    starts = [
        index
        for index, char in enumerate(text)
        if char == "{"
    ]
    for index in starts:
        try:
            value = json.loads(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError(
        "papers command did not return a JSON object"
    )


def _paper_source_rows(
    report: Mapping[str, object],
) -> list[dict[str, object]]:
    raw = report.get("papers")
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, object]] = []
    for paper in raw[:MAX_PAPERS]:
        if not isinstance(paper, Mapping):
            continue
        title = " ".join(
            str(paper.get("title") or "").split()
        )
        if not title:
            continue
        url = (
            paper.get("url")
            or paper.get("semantic_scholar_url")
            or paper.get("open_access_pdf_url")
        )
        doi = paper.get("doi") or paper.get("DOI")
        abstract = " ".join(
            str(paper.get("abstract") or "").split()
        )[:MAX_SOURCE_TEXT]
        providers = paper.get("providers")
        if not isinstance(providers, list):
            provider = paper.get("provider")
            providers = (
                [provider]
                if isinstance(provider, str)
                else []
            )
        rows.append({
            "source_id": f"S{len(rows) + 1}",
            "kind": "paper",
            "title": title[:500],
            "url": (
                str(url)[:2000]
                if isinstance(url, str)
                else None
            ),
            "doi": (
                str(doi)[:300]
                if isinstance(doi, str)
                else None
            ),
            "year": paper.get("year"),
            "abstract": abstract,
            "providers": [
                str(value)[:120]
                for value in providers[:4]
                if isinstance(value, str)
            ],
        })
    return rows


def _biomedical_source_rows(
    report: Mapping[str, object],
) -> list[dict[str, object]]:
    raw = report.get("results")
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, object]] = []
    for item in raw[:MAX_SOURCES]:
        if not isinstance(item, Mapping):
            continue
        title = " ".join(
            str(item.get("title") or "").split()
        )
        if not title:
            continue
        abstract = " ".join(
            str(item.get("abstract") or "").split()
        )[:MAX_SOURCE_TEXT]
        url = item.get("url")
        doi = item.get("doi")
        providers = item.get("providers")
        if not isinstance(providers, list):
            provider = item.get("provider")
            providers = (
                [provider]
                if isinstance(provider, str)
                else []
            )
        rows.append({
            "source_id": f"S{len(rows) + 1}",
            "kind": "paper",
            "title": title[:500],
            "url": (
                str(url)[:2000]
                if isinstance(url, str)
                else None
            ),
            "doi": (
                str(doi)[:300]
                if isinstance(doi, str)
                else None
            ),
            "year": item.get("year"),
            "abstract": abstract,
            "providers": [
                str(value)[:120]
                for value in providers[:4]
                if isinstance(value, str)
            ],
        })
    return rows


def _web_source_rows(
    result: Mapping[str, object],
) -> list[dict[str, object]]:
    raw = result.get("sources")
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, object]] = []
    for item in raw[:MAX_SOURCES]:
        if not isinstance(item, Mapping):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        rows.append({
            "source_id": f"S{len(rows) + 1}",
            "kind": "web",
            "title": (
                str(item.get("title") or url)[:500]
            ),
            "url": url[:2000],
            "source_kind": str(
                item.get("source_kind") or "web"
            )[:120],
        })
    return rows


def _verification(
    sources: list[dict[str, object]],
    *,
    route: str,
) -> dict[str, object]:
    if route in {
        "free_scholarly_papers",
        "free_biomedical_mesh",
    }:
        independent = set()
        for row in sources:
            doi = row.get("doi")
            url = row.get("url")
            title = row.get("title")
            token = doi or url or title
            if token:
                independent.add(str(token).casefold())
        count = len(independent)
        return {
            "status": (
                "verified_multi_evidence"
                if count >= 2
                else "limited_evidence"
            ),
            "method": "distinct scholarly records",
            "source_count": len(sources),
            "independent_evidence_count": count,
        }

    hosts = set()
    for row in sources:
        url = row.get("url")
        if not isinstance(url, str):
            continue
        try:
            host = (
                urlsplit(url).hostname
                or ""
            ).casefold()
        except ValueError:
            continue
        if host:
            hosts.add(host)
    return {
        "status": (
            "verified_multi_source"
            if len(hosts) >= 2
            else "limited_evidence"
        ),
        "method": "distinct grounded web hosts",
        "source_count": len(sources),
        "independent_evidence_count": len(hosts),
    }


def _evidence_digest(
    query: str,
    sources: list[dict[str, object]],
    verification: Mapping[str, object],
) -> str:
    if not sources:
        return (
            "Research completed without enough source evidence to "
            "produce a verified answer."
        )
    lines = [
        (
            f"Research evidence for: {query}\n"
            f"Verification: {verification.get('status')} "
            f"({verification.get('independent_evidence_count', 0)} "
            f"independent evidence source(s))."
        )
    ]
    for row in sources[:6]:
        sid = row.get("source_id")
        title = row.get("title")
        abstract = row.get("abstract")
        if abstract:
            lines.append(
                f"[{sid}] {title}: {abstract[:450]}"
            )
        else:
            lines.append(f"[{sid}] {title}")
    lines.append(
        "Use the source cards to inspect the underlying evidence."
    )
    return "\n\n".join(lines)


def _synthesis_prompt(
    query: str,
    sources: list[dict[str, object]],
) -> str:
    parts = [
        "Answer the research question using ONLY the source evidence below. "
        "Preserve uncertainty. Cite supporting sources inline as [S1], [S2], "
        "etc. Do not invent citations.\n"
        f"Question: {query}\n"
    ]
    for row in sources[:5]:
        sid = row.get("source_id")
        title = row.get("title")
        abstract = row.get("abstract") or ""
        parts.append(
            f"[{sid}] {title}\n{str(abstract)[:550]}"
        )
    return "\n\n".join(parts)[:3900]


def _maybe_synthesize(
    root: Path,
    query: str,
    sources: list[dict[str, object]],
    *,
    history: list[dict[str, object]] | None,
) -> tuple[str, dict[str, object] | None]:
    settings = DesktopChatSettingsStore(root).read()
    if (
        settings.get("gemini_free_tier_confirmed")
        is not True
        or not sources
    ):
        return "", None
    result = run_desktop_model_chat(
        root,
        _synthesis_prompt(query, sources),
        history=history,
    )
    reply = result.get("reply")
    if (
        result.get("status") == "completed"
        and isinstance(reply, str)
        and reply.strip()
    ):
        return reply.strip(), result
    return "", result



def _load_papers_artifact_from_cli_summary(
    root: Path,
    summary: Mapping[str, object],
) -> dict[str, object]:
    root = Path(root).resolve()
    run_id = summary.get("run_id")
    result_path = summary.get("result")

    if (
        not isinstance(run_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", run_id)
        or not isinstance(result_path, str)
    ):
        raise ValueError("invalid papers CLI summary")

    runs_root = (root / "runs").resolve()
    expected = (runs_root / run_id / "papers.json").resolve()
    supplied = Path(result_path).expanduser().resolve()

    if (
        supplied != expected
        or supplied.parent.parent != runs_root
        or supplied.name != "papers.json"
        or supplied.is_symlink()
        or not supplied.is_file()
        or supplied.stat().st_size > 3 * 1024 * 1024
    ):
        raise ValueError("unsafe or missing papers result artifact")

    try:
        artifact = json.loads(
            supplied.read_text(encoding="utf-8")
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        raise ValueError("invalid papers result artifact") from None

    if (
        not isinstance(artifact, dict)
        or artifact.get("schema_version") != 1
        or artifact.get("run_id") != run_id
        or not isinstance(artifact.get("question"), str)
        or not isinstance(artifact.get("papers"), list)
        or len(artifact["papers"]) > MAX_PAPERS
        or not isinstance(artifact.get("metrics"), Mapping)
    ):
        raise ValueError("invalid papers artifact contract")

    summary_count = summary.get("papers")
    if (
        type(summary_count) is not int
        or summary_count < 0
        or summary_count != len(artifact["papers"])
    ):
        raise ValueError("papers CLI summary/artifact count mismatch")

    summary_status = summary.get("status")
    if (
        not isinstance(summary_status, str)
        or artifact.get("status") != summary_status
    ):
        raise ValueError("papers CLI summary/artifact status mismatch")

    return artifact


def _run_papers_cli(
    root: Path,
    query: str,
    *,
    timeout_seconds: int = 120,
) -> dict[str, object]:
    python = root / ".venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError(
            "SIRA virtual environment Python was not found"
        )
    proc = subprocess.run(
        [
            str(python),
            "sira.py",
            "papers",
            query,
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "free scholarly papers command failed"
        )
    summary = _parse_cli_json(proc.stdout)
    return _load_papers_artifact_from_cli_summary(
        root,
        summary,
    )


def _general_web_environ(
    root: Path,
) -> dict[str, str] | None:
    research = DesktopResearchSettingsStore(root).read()
    chat = DesktopChatSettingsStore(root).read()
    if (
        research.get(
            "gemini_search_zero_cost_confirmed"
        )
        is not True
        or chat.get(
            "gemini_free_tier_confirmed"
        )
        is not True
    ):
        return None
    env = dict(os.environ)
    env[
        "SIRA_GEMINI_FREE_TIER_CONFIRMED"
    ] = "true"
    env[
        SEARCH_CONFIRM_ENV
    ] = "true"
    return env


def research_settings_status(
    root: Path,
) -> dict[str, object]:
    root = Path(root).resolve()
    settings = DesktopResearchSettingsStore(root).read()
    chat = DesktopChatSettingsStore(root).read()
    return {
        "schema": "sira.desktop_research_status.v1",
        "explicit_only": True,
        "free_first": True,
        "scholarly_route": (
            "Semantic Scholar -> Crossref -> arXiv"
        ),
        "biomedical_route": (
            "Europe PMC / PubMed free mesh"
        ),
        "general_web_route": (
            "Gemini Search grounding only after separate confirmation"
        ),
        "gemini_free_tier_confirmed": bool(
            chat.get("gemini_free_tier_confirmed")
        ),
        "gemini_search_zero_cost_confirmed": bool(
            settings.get(
                "gemini_search_zero_cost_confirmed"
            )
        ),
        "paid_spending_authority": False,
        "runtime_control_authority": False,
        "promotion_authority": False,
    }


def run_research_job(
    root: Path,
    job_id: str,
    *,
    history: list[dict[str, object]] | None = None,
    papers_runner: Callable[
        [Path, str], dict[str, object]
    ] | None = None,
    biomedical_runner: Callable[
        [Path, str], dict[str, object]
    ] | None = None,
    web_runner: Callable[..., dict[str, object]] | None = None,
) -> dict[str, object]:
    root = Path(root).resolve()
    store = DesktopResearchJobStore(root)
    job = store.read(job_id)
    if job is None:
        raise ValueError("research job not found")
    query = _clean_query(str(job["query"]))
    domain = str(job["domain"])

    _update(
        store,
        job,
        status="running",
        progress="planning_free_first_route",
        progress_percent=10,
    )
    try:
        plan_domain = (
            "biomedical"
            if domain == "biomedical"
            else "scholarly"
            if domain == "scholarly"
            else "general_web"
        )
        mesh_plan = research_mesh_plan(
            root,
            plan_domain,
            allow_metered=False,
            persist=True,
        )
        job["free_first_plan"] = mesh_plan

        sources: list[dict[str, object]] = []
        answer = ""
        route = ""
        model_synthesis = None
        metrics = {
            "api_requests": 0,
            "metered_model_requests": 0,
            "paid_requests": 0,
        }

        if domain == "biomedical":
            _update(
                store,
                job,
                progress="searching_free_biomedical_sources",
                progress_percent=35,
            )
            runner = (
                biomedical_runner
                or (
                    lambda root_value, query_value:
                    search_biomedical_free(
                        root_value,
                        query_value,
                        max_results=3,
                    )
                )
            )
            report = runner(root, query)
            sources = _biomedical_source_rows(report)
            route = "free_biomedical_mesh"
            raw_metrics = report.get("metrics")
            if isinstance(raw_metrics, Mapping):
                metrics["api_requests"] = int(
                    raw_metrics.get("api_requests") or 0
                )
        elif domain == "scholarly":
            _update(
                store,
                job,
                progress="searching_free_scholarly_sources",
                progress_percent=35,
            )
            runner = (
                papers_runner
                or (
                    lambda root_value, query_value:
                    _run_papers_cli(
                        root_value,
                        query_value,
                    )
                )
            )
            report = runner(root, query)
            sources = _paper_source_rows(report)
            route = "free_scholarly_papers"
            raw_metrics = report.get("metrics")
            if isinstance(raw_metrics, Mapping):
                metrics["api_requests"] = int(
                    raw_metrics.get("api_requests") or 0
                )
        else:
            _update(
                store,
                job,
                progress="checking_general_web_guard",
                progress_percent=25,
            )
            research_env = _general_web_environ(root)
            if research_env is None:
                _update(
                    store,
                    job,
                    status="blocked",
                    progress="search_confirmation_required",
                    progress_percent=100,
                    route="guarded_general_web",
                    answer=(
                        "General web research is blocked until both "
                        "Gemini zero-cost chat use and the separate "
                        "Gemini Search zero-cost/no-charge confirmation "
                        "are enabled. SIRA will not enable billing."
                    ),
                )
                return store.read(job_id) or job

            _update(
                store,
                job,
                progress="running_grounded_web_search",
                progress_percent=45,
            )
            runner = web_runner or run_gemini_research
            report = runner(
                root,
                query,
                mode=MODE_GOOGLE_SEARCH,
                environ=research_env,
            )
            if report.get("status") != "completed":
                error = report.get("error")
                code = (
                    error.get("code")
                    if isinstance(error, Mapping)
                    else "research_provider_unavailable"
                )
                _update(
                    store,
                    job,
                    status="blocked",
                    progress="web_search_blocked",
                    progress_percent=100,
                    route="guarded_general_web",
                    answer=(
                        f"General web research was blocked by "
                        f"SIRA's provider/cost policy: {code}."
                    ),
                )
                return store.read(job_id) or job
            result = report.get("result")
            if not isinstance(result, Mapping):
                raise ValueError(
                    "grounded research result missing"
                )
            sources = _web_source_rows(result)
            answer = str(result.get("text") or "").strip()
            route = "guarded_gemini_search"
            raw_metrics = report.get("metrics")
            if isinstance(raw_metrics, Mapping):
                metrics["api_requests"] = int(
                    raw_metrics.get("api_requests") or 0
                )
                metrics["metered_model_requests"] = (
                    1
                    if metrics["api_requests"] > 0
                    else 0
                )

        _update(
            store,
            job,
            progress="verifying_independent_evidence",
            progress_percent=70,
        )
        verification = _verification(
            sources,
            route=route,
        )

        if not answer:
            _update(
                store,
                job,
                progress="synthesizing_source_grounded_answer",
                progress_percent=82,
            )
            synthesized, model_result = _maybe_synthesize(
                root,
                query,
                sources,
                history=history,
            )
            if synthesized:
                answer = synthesized
            else:
                answer = _evidence_digest(
                    query,
                    sources,
                    verification,
                )
            if isinstance(model_result, Mapping):
                model_synthesis = {
                    "status": model_result.get("status"),
                    "intent": model_result.get("intent"),
                    "metrics": model_result.get("metrics"),
                    "artifact": model_result.get("artifact"),
                }
                model_metrics = model_result.get("metrics")
                if isinstance(model_metrics, Mapping):
                    metrics[
                        "metered_model_requests"
                    ] += int(
                        model_metrics.get(
                            "api_requests"
                        )
                        or 0
                    )

        final_status = (
            "completed"
            if verification["status"].startswith(
                "verified_"
            )
            else "limited"
        )
        _update(
            store,
            job,
            status=final_status,
            progress="completed",
            progress_percent=100,
            route=route,
            answer=answer,
            sources=sources,
            verification=verification,
            model_synthesis=model_synthesis,
            metrics=metrics,
        )
        return store.read(job_id) or job

    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        _update(
            store,
            job,
            status="failed",
            progress="failed",
            progress_percent=100,
            answer=(
                "Research failed safely. No protected or runtime "
                "action was performed."
            ),
            error_code=type(exc).__name__,
        )
        return store.read(job_id) or job


def recover_interrupted_research_jobs(
    root: Path,
) -> dict[str, object]:
    root = Path(root).resolve()
    store = DesktopResearchJobStore(root)
    recovered: list[str] = []
    for job in store.list():
        if job.get("status") not in {"queued", "running"}:
            continue
        job["status"] = "failed"
        job["progress"] = "interrupted_by_desktop_restart"
        job["progress_percent"] = 100
        job["answer"] = (
            "This research job was interrupted when the Desktop "
            "process stopped. It was not automatically retried, to "
            "avoid duplicate external requests."
        )
        store.save(job)
        recovered.append(str(job.get("job_id")))
    return {
        "schema": "sira.desktop_research_recovery.v1",
        "recovered_count": len(recovered),
        "job_ids": recovered,
        "automatic_retry": False,
    }


def create_research_job(
    root: Path,
    message: str,
) -> dict[str, object]:
    root = Path(root).resolve()
    query = _clean_query(message)
    domain = classify_research_domain(query)
    return DesktopResearchJobStore(root).create(
        query,
        domain,
    )
