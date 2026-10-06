"""Cloud document contracts and provenance-preserving evidence runs."""
from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from time import perf_counter
from typing import Protocol
from urllib.parse import urlsplit

from .models import ProviderError, canonical_url, utc_now
from .storage import Cache, RunStore, code_digest, write_json
from .reports import evidence_quotes, render_html, render_markdown


def public_url(value: str) -> str:
    """Reject obviously non-public inputs. No client-side URL fetch or DNS lookup."""
    url = canonical_url(value)
    parts = urlsplit(url)
    host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    if any(c in url for c in "\\<>\"`") or parts.port not in (None, 80 if parts.scheme == "http" else 443):
        raise ValueError("Unsupported URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if ("." not in host or host.endswith((".local", ".localhost", ".internal", ".invalid", ".test"))
                or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)
                or not re.search(r"[a-z]", host.rsplit(".", 1)[-1])):
            raise ValueError("Public hostname required")
    else:
        if not address.is_global:
            raise ValueError("Public address required")
    return url


@dataclass(frozen=True)
class Document:
    url: str
    text: str
    retrieved_at: str

    def __post_init__(self):
        object.__setattr__(self, "url", public_url(self.url))
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 200000:
            raise ValueError("Document must contain 1..200000 text characters")
        self.text.encode("utf-8")  # Reject lone JSON surrogates before caching or writing.
        if not isinstance(self.retrieved_at, str) or not self.retrieved_at:
            raise ValueError("Missing retrieval time")

    @property
    def content_sha256(self):
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExtractBatch:
    documents: tuple[Document, ...]
    failures: dict[str, str]
    api_requests: int = 0
    credits: float | None = 0


class ExtractProvider(Protocol):
    name: str
    cache_namespace: str

    def read(self, urls: tuple[str, ...]) -> ExtractBatch: ...


def load_parent(root: Path, run_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Expected the 32-character search run ID")
    path = root / "runs" / run_id / "result.json"
    if path.resolve().parent.parent != (root / "runs").resolve():
        raise ValueError("Parent run must stay inside runs/")
    if not path.is_file() or path.stat().st_size > 3 * 1024 * 1024:
        raise ValueError("Search result missing or too large; check the run ID")
    raw = path.read_bytes()
    try:
        parent = json.loads(raw)
        if (parent.get("schema_version") != 1 or parent.get("run_id") != run_id
                or not isinstance(parent.get("question"), str)
                or not isinstance(parent.get("sources"), list) or len(parent["sources"]) > 5):
            raise ValueError("Invalid search result")
        ids = set()
        for source in parent["sources"]:
            if (not isinstance(source, dict) or not re.fullmatch(r"S[1-5]", source.get("id", ""))
                    or source["id"] in ids or not isinstance(source.get("url"), str)
                    or not isinstance(source.get("title"), str)):
                raise ValueError("Invalid source record")
            ids.add(source["id"])
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise ValueError("Invalid parent result format") from None
    return parent, hashlib.sha256(raw).hexdigest()


def _assemble_source_evidence(parent, documents, outcomes, run, reader_name, error):
    """Write source texts and assemble final per-source evidence and status."""
    records, evidence = [], []
    for source in parent["sources"]:
        record = {"source_id": source["id"], "url": source["url"], "title": source["title"],
                  "search_retrieved_at": source.get("retrieved_at"), "trust": "untrusted",
                  "verification": "not_read", "status": outcomes.get(source["id"], "missing_result")}
        try:
            doc = documents.get(public_url(source["url"]))
        except (ValueError, UnicodeError):
            doc = None
        if doc:
            text_path = Path("documents") / (source["id"] + ".txt")
            (run.path / text_path).parent.mkdir(exist_ok=True)
            (run.path / text_path).write_text(doc.text, encoding="utf-8", newline="")
            record.update(status="read", verification="provider_text_retrieved",
                          content_sha256=doc.content_sha256, retrieved_at=doc.retrieved_at,
                          text_path=text_path.as_posix(), content_origin=reader_name)
            evidence.extend(evidence_quotes(parent["question"], source["id"], doc))
        records.append(record)
        run.event("source_finished", source_id=source["id"], status=record["status"])
    read_count = sum(r["status"] == "read" for r in records)
    status = ("cancelled" if error and error["code"] == "user_cancelled" else
              "completed" if read_count and read_count == len(records) else
              "partial" if read_count else "no_sources" if not records else "failed")
    integrity = sum(documents[q["url"]].text[q["start"]:q["end"]] == q["quote"] for q in evidence)
    return records, evidence, read_count, status, integrity


def read_run(root: Path, parent_id: str, reader: ExtractProvider, *, use_cache=True):
    parent, parent_sha = load_parent(root, parent_id)
    start = perf_counter()
    run = RunStore(root)
    run.event("run_started", operation="read", parent_run_id=parent_id, reader=reader.name)
    cache = Cache(root / ".cache" / "documents", 86400)
    documents, outcomes, pending = {}, {}, []
    cache_hits, api_requests, credits, error = 0, 0, 0, None
    for source in parent["sources"]:
        try:
            url = public_url(source["url"])
        except (ValueError, UnicodeError):
            outcomes[source["id"]] = "unsafe_url"
            continue
        key = json.dumps([reader.cache_namespace, url])
        cached = cache.get(key) if use_cache else None
        if cached is not None:
            try:
                doc = Document(**cached)
                if doc.url != url:
                    raise ValueError("Cache URL mismatch")
                documents[url] = doc
                cache_hits += 1
                continue
            except (ValueError, TypeError):
                run.event("cache_invalid", source_id=source["id"])
        if url not in pending:
            pending.append(url)
    if pending:
        run.event("reader_started", url_count=len(pending))
        try:
            batch = reader.read(tuple(pending))
            api_requests, credits = batch.api_requests, batch.credits
            documents.update({doc.url: doc for doc in batch.documents if doc.url in pending})
            for url, doc in documents.items():
                if use_cache and url in pending:
                    cache.put(json.dumps([reader.cache_namespace, url]), asdict(doc))
            for source in parent["sources"]:
                if source["id"] not in outcomes:
                    url = public_url(source["url"])
                    if url not in documents:
                        outcomes[source["id"]] = batch.failures.get(url, "missing_result")
            run.event("reader_finished", api_requests=api_requests)
        except ProviderError as exc:
            api_requests, credits = int(exc.request_sent), None if exc.request_sent else 0
            error = {"code": exc.code, "retry_after_seconds": exc.retry_after}
            for source in parent["sources"]:
                if source["id"] not in outcomes and public_url(source["url"]) in pending:
                    outcomes[source["id"]] = exc.code
            run.event("reader_failed", **error)
        except KeyboardInterrupt:
            api_requests, credits, error = None, None, {"code": "user_cancelled"}
            run.event("reader_cancelled")
    records, evidence, read_count, status, integrity = _assemble_source_evidence(
        parent, documents, outcomes, run, reader.name, error,
    )
    result = {
        "schema_version": 1, "kind": "source_evidence", "run_id": run.run_id,
        "parent_run_id": parent_id, "parent_sha256": parent_sha, "code_sha256": code_digest(),
        "created_at": utc_now(), "question": parent["question"], "reader": reader.name,
        "reader_config": reader.cache_namespace, "cache_ttl_seconds": 86400,
        "status": status, "error": error, "documents": records, "evidence": evidence,
        "metrics": {"sources_requested": len(records), "sources_read": read_count,
                    "source_read_success": read_count / len(records) if records else None,
                    "failures": len(records) - read_count, "cache_hits": cache_hits,
                    "api_requests": api_requests, "reported_credits": credits,
                    "latency_seconds": round(perf_counter() - start, 6),
                    "quote_integrity": integrity / len(evidence) if evidence else None,
                    "factual_accuracy": None, "citation_correctness": None,
                    "citation_coverage": None, "source_quality": None, "source_diversity": None},
    }
    write_json(run.path / "evidence.json", result)
    (run.path / "report.md").write_text(render_markdown(result), encoding="utf-8")
    (run.path / "report.html").write_text(render_html(result), encoding="utf-8")
    run.event("run_finished", status=status)
    return run.path
