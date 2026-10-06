"""Convert scholarly metadata runs into provenance-checked evidence runs."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
from time import perf_counter

from . import __version__
from .models import ProviderError, utc_now
from .reading import Document, ExtractProvider, public_url
from .reports import evidence_quotes, render_html, render_markdown
from .storage import Cache, RunStore, code_digest, write_json


MAX_PAPERS = 3
MAX_PARENT_BYTES = 3 * 1024 * 1024


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def load_paper_parent(root: Path, run_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Expected the 32-character paper run ID")
    runs = (root / "runs").resolve()
    run_dir = (runs / run_id).resolve()
    path = (run_dir / "papers.json").resolve()
    if (run_dir.parent != runs or not _inside(path, runs) or path.is_symlink()
            or not path.is_file() or path.stat().st_size > MAX_PARENT_BYTES):
        raise ValueError("Paper result missing or too large; check the run ID")
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
        papers = data["papers"]
        if (data.get("schema_version") != 1 or data.get("run_id") != run_id
                or data.get("output_kind") != "scholarly_metadata_not_fact_checked"
                or not isinstance(data.get("question"), str)
                or not 1 <= len(data["question"].strip()) <= 500
                or data.get("status") not in ("completed", "no_results")
                or not isinstance(papers, list) or len(papers) > MAX_PAPERS):
            raise ValueError
        seen = set()
        for row in papers:
            if (not isinstance(row, dict) or not isinstance(row.get("paper_id"), str)
                    or not row["paper_id"].strip() or len(row["paper_id"]) > 256
                    or row["paper_id"] in seen
                    or not isinstance(row.get("title"), str) or not 1 <= len(row["title"].strip()) <= 4000
                    or not isinstance(row.get("url"), str)
                    or not isinstance(row.get("retrieved_at"), str) or not row["retrieved_at"].strip()
                    or row.get("abstract") is not None and (not isinstance(row.get("abstract"), str)
                                                             or len(row["abstract"]) > 100_000)
                    or row.get("open_access_pdf_url") is not None and not isinstance(row.get("open_access_pdf_url"), str)):
                raise ValueError
            public_url(row["url"])
            if row.get("open_access_pdf_url") is not None:
                public_url(row["open_access_pdf_url"])
            seen.add(row["paper_id"])
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid paper result format") from None
    return data, hashlib.sha256(raw).hexdigest()


def _process_abstracts(parent, documents, outcomes, origins, pdf_for_source):
    for index, paper in enumerate(parent["papers"], start=1):
        sid = f"P{index}"
        abstract = paper.get("abstract")
        if isinstance(abstract, str) and abstract.strip():
            try:
                documents[sid] = Document(public_url(paper["url"]), abstract.strip(), paper["retrieved_at"])
                origins[sid] = "paper_abstract"
            except (KeyError, TypeError, ValueError, UnicodeError):
                outcomes[sid] = "invalid_abstract"
        elif paper.get("open_access_pdf_url"):
            pdf_for_source[sid] = public_url(paper["open_access_pdf_url"])
        else:
            outcomes[sid] = "no_readable_content"


def _assemble_records(parent, run, documents, origins, outcomes, evidence):
    records = []
    for index, paper in enumerate(parent["papers"], start=1):
        sid = f"P{index}"
        title = paper["title"].strip()
        evidence_title = title if len(title) <= 500 else title[:497].rstrip() + "..."
        record = {
            "source_id": sid, "paper_id": paper["paper_id"], "url": paper["url"],
            "title": evidence_title, "doi": paper.get("doi"),
            "providers": paper.get("providers") or [paper.get("provider", "unknown")],
            "paper_retrieved_at": paper.get("retrieved_at"), "trust": "untrusted",
            "verification": "not_read", "status": outcomes.get(sid, "missing_result"),
        }
        document = documents.get(sid)
        if document is not None:
            text_path = Path("documents") / f"{sid}.txt"
            (run.path / text_path).parent.mkdir(exist_ok=True)
            (run.path / text_path).write_text(document.text, encoding="utf-8", newline="")
            record.update(status="read", verification="provider_text_retrieved" if origins[sid] == "open_access_pdf" else "paper_abstract_retrieved", content_sha256=document.content_sha256, retrieved_at=document.retrieved_at, text_path=text_path.as_posix(), content_origin=origins[sid], evidence_url=document.url)
            evidence.extend(evidence_quotes(parent["question"], sid, document))
        records.append(record)
        run.event("source_finished", source_id=sid, status=record["status"], origin=origins.get(sid))
    return records


def paper_read_run(root: Path, parent_id: str, pdf_reader: ExtractProvider, *, use_cache=True):
    parent, parent_sha = load_paper_parent(root, parent_id)
    start, run = perf_counter(), RunStore(root)
    run.event("run_started", operation="paper_read", parent_run_id=parent_id, reader=pdf_reader.name)
    cache = Cache(root / ".cache" / "paper_documents", 86400)
    documents, outcomes, origins, pdf_for_source = {}, {}, {}, {}
    cache_hits = api_requests = 0
    error = None
    _process_abstracts(parent, documents, outcomes, origins, pdf_for_source)
    pending, pdf_docs_by_url = [], {}
    for sid, url in pdf_for_source.items():
        key = json.dumps([pdf_reader.cache_namespace, url], ensure_ascii=False)
        cached = cache.get(key) if use_cache else None
        if cached is not None:
            try:
                document = Document(**cached)
                if document.url != url: raise ValueError("cache URL mismatch")
                pdf_docs_by_url[url] = document
                cache_hits += 1
                continue
            except (TypeError, ValueError, UnicodeError): run.event("cache_invalid", source_id=sid)
        if url not in pending: pending.append(url)
    if pending:
        run.event("pdf_reader_started", url_count=len(pending))
        try:
            batch = pdf_reader.read(tuple(pending))
            api_requests = batch.api_requests
            pdf_docs_by_url.update({doc.url: doc for doc in batch.documents if doc.url in pending})
            for url, document in pdf_docs_by_url.items():
                if use_cache and url in pending: cache.put(json.dumps([pdf_reader.cache_namespace, url], ensure_ascii=False), asdict(document))
            for sid, url in pdf_for_source.items():
                if url not in pdf_docs_by_url and sid not in outcomes: outcomes[sid] = batch.failures.get(url, "missing_result")
            run.event("pdf_reader_finished", api_requests=api_requests)
        except ProviderError as exc:
            api_requests = exc.request_count
            for sid, url in pdf_for_source.items():
                if url in pending and sid not in outcomes: outcomes[sid] = exc.code
            run.event("pdf_reader_failed", code=exc.code, retry_after_seconds=exc.retry_after)
        except KeyboardInterrupt:
            error = {"code": "user_cancelled"}
            for sid in pdf_for_source: outcomes.setdefault(sid, "user_cancelled")
            run.event("reader_cancelled")
    for sid, url in pdf_for_source.items():
        if sid not in documents and url in pdf_docs_by_url:
            documents[sid] = pdf_docs_by_url[url]
            origins[sid] = "open_access_pdf"
    evidence = []
    records = _assemble_records(parent, run, documents, origins, outcomes, evidence)
    read_count = sum(item["status"] == "read" for item in records)
    status = ("cancelled" if error and error["code"] == "user_cancelled" else "completed" if records and read_count == len(records) else "partial" if read_count else "no_sources" if not records else "failed")
    evidence_docs = {sid: doc for sid, doc in documents.items()}
    integrity = sum(evidence_docs[q["source_id"]].text[q["start"]:q["end"]] == q["quote"] for q in evidence)
    result = {
        "schema_version": 1, "kind": "source_evidence", "evidence_origin": "scholarly_paper", "run_id": run.run_id, "parent_run_id": parent_id, "parent_artifact": "papers.json", "parent_sha256": parent_sha, "code_sha256": code_digest(), "created_at": utc_now(), "question": parent["question"], "reader": pdf_reader.name, "reader_config": pdf_reader.cache_namespace, "cache_ttl_seconds": 86400, "status": status, "error": error, "documents": records, "evidence": evidence,
        "metrics": {
            "sources_requested": len(records), "sources_read": read_count, "source_read_success": read_count / len(records) if records else None, "abstracts_used": sum(value == "paper_abstract" for value in origins.values()), "pdfs_used": sum(value == "open_access_pdf" for value in origins.values()), "failures": len(records) - read_count, "cache_hits": cache_hits, "api_requests": None if status == "cancelled" else api_requests, "reported_credits": 0, "latency_seconds": round(perf_counter() - start, 6), "quote_integrity": integrity / len(evidence) if evidence else None, "factual_accuracy": None, "citation_correctness": None, "citation_coverage": None, "source_quality": None, "source_diversity": None,
        }, "sira_version": __version__,
    }
    write_json(run.path / "evidence.json", result)
    (run.path / "report.md").write_text(render_markdown(result), encoding="utf-8")
    (run.path / "report.html").write_text(render_html(result), encoding="utf-8")
    run.event("run_finished", status=status)
    return run.path
