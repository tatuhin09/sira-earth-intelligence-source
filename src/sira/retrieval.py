"""Deterministic local passage retrieval over provenance-checked evidence runs."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .reading import public_url


MAX_DOCUMENTS = 5
MAX_DOCUMENT_CHARS = 200000
MAX_PASSAGES = 12
MAX_PASSAGE_CHARS = 1200
MAX_PACKET_CHARS = 12000


@dataclass(frozen=True)
class LoadedDocument:
    source_id: str
    title: str
    url: str
    text: str
    content_sha256: str


@dataclass(frozen=True)
class Passage:
    id: str
    source_id: str
    title: str
    url: str
    text: str
    start: int
    end: int
    content_sha256: str

    def as_dict(self):
        return {"id": self.id, "source_id": self.source_id, "title": self.title,
                "url": self.url, "text": self.text, "start": self.start, "end": self.end,
                "content_sha256": self.content_sha256}


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_run_path(root: Path, run_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Expected the 32-character evidence run ID")
    run_dir = (root / "runs" / run_id).resolve()
    runs_dir = (root / "runs").resolve()
    if not _inside(run_dir, runs_dir) or run_dir.parent != runs_dir:
        raise ValueError("Evidence run must stay inside runs/")
    return run_dir, runs_dir


def _validate_evidence_format(evidence_path: Path, run_id: str):
    if not evidence_path.is_file() or evidence_path.is_symlink() or evidence_path.stat().st_size > 3 * 1024 * 1024:
        raise ValueError("Evidence result missing or too large; check the run ID")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw)
        records = evidence["documents"]
        if (evidence.get("schema_version") != 1 or evidence.get("kind") != "source_evidence"
                or evidence.get("run_id") != run_id or not isinstance(evidence.get("question"), str)
                or not 1 <= len(evidence["question"].strip()) <= 500 or not isinstance(records, list)
                or len(records) > MAX_DOCUMENTS):
            raise ValueError
        return evidence, raw, records
    except (ValueError, KeyError, TypeError, RecursionError):
        raise ValueError("Invalid evidence result format") from None


def _validate_parent_integrity(evidence: dict, runs_dir: Path):
    parent_id, parent_sha = evidence.get("parent_run_id"), evidence.get("parent_sha256")
    if (not isinstance(parent_id, str) or not re.fullmatch(r"[0-9a-f]{32}", parent_id)
            or not isinstance(parent_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", parent_sha)):
        raise ValueError("Invalid evidence parent reference")
    parent_artifact = evidence.get("parent_artifact", "result.json")
    if parent_artifact not in ("result.json", "papers.json"):
        raise ValueError("Invalid evidence parent artifact")
    parent_path = (runs_dir / parent_id / parent_artifact).resolve()
    if (not _inside(parent_path, runs_dir) or parent_path.is_symlink() or not parent_path.is_file()
            or parent_path.stat().st_size > 3 * 1024 * 1024
            or hashlib.sha256(parent_path.read_bytes()).hexdigest() != parent_sha):
        raise ValueError("Evidence parent integrity check failed")


def load_evidence_run(root: Path, run_id: str):
    """Load evidence and fail closed if a document changed after the evidence run."""
    run_dir, runs_dir = _validate_run_path(root, run_id)
    evidence, raw, records = _validate_evidence_format(run_dir / "evidence.json", run_id)
    _validate_parent_integrity(evidence, runs_dir)

    documents, seen = [], set()
    for record in records:
        if not isinstance(record, dict) or record.get("status") != "read":
            continue
        source_id = record.get("source_id")
        relative = record.get("text_path")
        digest = record.get("content_sha256")
        if (not isinstance(source_id, str) or not re.fullmatch(r"(?:S[1-5]|P[1-3])", source_id)
                or source_id in seen or not isinstance(relative, str)
                or relative != f"documents/{source_id}.txt"
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(record.get("title"), str) or not 1 <= len(record["title"].strip()) <= 500
                or not isinstance(record.get("url"), str) or len(record["url"]) > 2048):
            raise ValueError("Invalid evidence document record")
        path = (run_dir / relative).resolve()
        if not _inside(path, run_dir / "documents") or path.is_symlink() or not path.is_file():
            raise ValueError("Unsafe or missing evidence document path")
        data = path.read_bytes()
        if len(data) > MAX_DOCUMENT_CHARS * 4 or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Evidence document integrity check failed")
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            raise ValueError("Evidence document is not valid UTF-8") from None
        if not text.strip() or len(text) > MAX_DOCUMENT_CHARS:
            raise ValueError("Evidence document size is invalid")
        try:
            url = public_url(record.get("evidence_url", record["url"]))
        except (ValueError, UnicodeError):
            raise ValueError("Invalid evidence source URL") from None
        documents.append(LoadedDocument(source_id, record["title"], url, text, digest))
        seen.add(source_id)
    if not documents:
        raise ValueError("Evidence run has no readable verified documents")
    return evidence, hashlib.sha256(raw).hexdigest(), tuple(documents)


def _terms(text: str):
    return set(re.findall(r"[\w]{2,}", text.casefold(), flags=re.UNICODE))


def _spans(text: str):
    """Paragraph-sized exact spans, splitting oversized paragraphs without rewriting text."""
    spans = []
    for match in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", text, re.DOTALL):
        start, end = match.span()
        while end - start > MAX_PASSAGE_CHARS:
            cut = text.rfind(" ", start, start + MAX_PASSAGE_CHARS + 1)
            if cut <= start:
                cut = start + MAX_PASSAGE_CHARS
            spans.append((start, cut))
            start = cut
            while start < end and text[start].isspace():
                start += 1
        if end > start:
            spans.append((start, end))
    return spans


def build_evidence_packet(question: str, documents: tuple[LoadedDocument, ...]):
    query = _terms(question)
    candidates = []
    for doc_index, document in enumerate(documents):
        title_terms = _terms(document.title)
        for span_index, (start, end) in enumerate(_spans(document.text)):
            text = document.text[start:end]
            terms = _terms(text)
            overlap = len(query & terms)
            score = overlap * 10 + len(query & title_terms) * 2
            candidates.append((-score, doc_index, span_index, document, start, end, text))
    candidates.sort(key=lambda item: item[:3])
    selected, total = [], 0
    for _, _, _, document, start, end, passage_text in candidates:
        if len(selected) >= MAX_PASSAGES or total + len(passage_text) > MAX_PACKET_CHARS:
            continue
        selected.append(Passage(f"E{len(selected) + 1}", document.source_id, document.title,
                                document.url, passage_text, start, end,
                                hashlib.sha256(passage_text.encode()).hexdigest()))
        total += len(passage_text)
    return tuple(selected)
