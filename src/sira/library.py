"""Local Library v1: a large, read-only, searchable document store (tier: raw/trusted corpus).

The owner copies documents (official docs, standards, papers, books, notes) into
``<library_root>/collections/<collection>/<source>/...`` on any disk, including a big
secondary volume. SIRA scans them, extracts text and builds a SQLite FTS5 index with
per-chunk provenance (collection, source, file, offset, sha256). Nothing here is
"verified knowledge": library text is data, never instructions, and a claim only becomes
verified through the existing evidence-gated knowledge pipeline. A collection's trust class
is owner-set metadata describing how much a source deserves to be read as evidence.

Safety: no network, no model, no code execution; symlinks and special files are never
followed; files with secret-like content are skipped (not indexed); size, depth and disk-space
bounds; indexing is resumable and time-bounded so a 50 GB library can be processed in
repeated runs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import time
from typing import Any, Iterator, Mapping
from uuid import uuid4

from .pdf_extraction import PdfExtractionError, extract_pdf_text

CONFIG_SCHEMA = "sira.library_config.v1"
COLLECTION_SCHEMA = "sira.library_collection.v1"
DB_VERSION = 1
TRUST_CLASSES = ("official_documentation", "technical_standard", "scholarly_publication",
                 "reference_book", "reference_other", "personal_notes", "unknown")
EVIDENCE_ELIGIBLE = frozenset({"official_documentation", "technical_standard",
                               "scholarly_publication", "reference_book"})
TEXT_SUFFIXES = frozenset({".txt", ".md", ".rst", ".markdown"})
HTML_SUFFIXES = frozenset({".html", ".htm", ".xhtml"})
PDF_SUFFIXES = frozenset({".pdf"})
ALLOWED_SUFFIXES = TEXT_SUFFIXES | HTML_SUFFIXES | PDF_SUFFIXES
DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 8_000_000
MAX_DEPTH = 14
MIN_FREE_BYTES = 2 * 1024 ** 3
CHUNK_TARGET = 900
CHUNK_MAX = 1800
SNIPPET_CHARS = 700
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SECRET = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----|AIza[0-9A-Za-z_\-]{30,}|"
    rb"\bsk-[A-Za-z0-9]{20,}|\bghp_[A-Za-z0-9]{30,}|\bxox[abp]-[A-Za-z0-9-]{20,}|"
    rb"\b(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[\"']?[A-Za-z0-9._/+\-=]{16,}",
    re.IGNORECASE)


class LibraryError(ValueError):
    """Invalid library configuration or use."""


# ---------------------------------------------------------------- config ---
def _config_path(repo_root: Path) -> Path:
    return Path(repo_root) / "memory" / "library" / "config.json"


def _check_root(repo_root: Path, library_root: Path) -> Path:
    if not library_root.is_absolute():
        raise LibraryError("library path must be absolute")
    resolved = library_root.resolve()
    repo = Path(repo_root).resolve()
    if not resolved.is_dir():
        raise LibraryError("library path is not an existing directory")
    if resolved == Path(resolved.anchor):
        raise LibraryError("library path may not be the filesystem root")
    if resolved == repo or repo.is_relative_to(resolved) or resolved.is_relative_to(repo):
        raise LibraryError("library must be outside the SIRA repository (and not contain it)")
    return resolved


def init_library(repo_root: Path, library_root: Path, *,
                 max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> dict:
    """Create the library layout and save the owner config (outside candidate code)."""
    if type(max_file_bytes) is not int or not 1 << 20 <= max_file_bytes <= 200 << 20:
        raise LibraryError("max_file_bytes must be 1 MiB..200 MiB")
    root = _check_root(repo_root, Path(library_root))
    for part in ("collections", "index"):
        (root / part).mkdir(exist_ok=True)
    readme = root / "collections" / "README.txt"
    if not readme.exists():
        readme.write_text(
            "Put documents here as collections/<collection>/<source>/...\n"
            "A <source> is one independent origin (for example python-docs or rfc-editor).\n"
            "Independence between sources is what lets two documents corroborate a claim.\n"
            "Supported: .txt .md .rst .html .htm .xhtml .markdown .pdf.\n"
            "PDF indexing uses Poppler (pdftotext) when available, with a simple-PDF fallback; "
            "image-only PDFs need OCR.\n"
            "Set trust with tools/sira_library.py set-trust.\n",
            encoding="utf-8")
    config = Path(repo_root) / "memory" / "library"
    if config.is_symlink() or Path(repo_root, "memory").is_symlink():
        raise LibraryError("unsafe config directory")
    config.mkdir(parents=True, exist_ok=True)
    temporary = config / f".config.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps({"schema": CONFIG_SCHEMA, "library_root": str(root),
                                     "max_file_bytes": max_file_bytes}), encoding="utf-8")
    os.replace(temporary, _config_path(repo_root))
    return load_config(repo_root)


def load_config(repo_root: Path) -> dict:
    path = _config_path(repo_root)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 4096:
        raise LibraryError("library not initialised (run init)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("schema") != CONFIG_SCHEMA
                or set(data) != {"schema", "library_root", "max_file_bytes"}
                or type(data["max_file_bytes"]) is not int
                or not 1 << 20 <= data["max_file_bytes"] <= 200 << 20
                or not isinstance(data["library_root"], str)):
            raise ValueError("invalid")
        root = _check_root(repo_root, Path(data["library_root"]))
    except (OSError, ValueError, UnicodeError) as exc:
        raise LibraryError(f"library config invalid: {exc}") from exc
    return {"library_root": root, "max_file_bytes": data["max_file_bytes"]}


def set_trust(repo_root: Path, collection: str, trust: str, description: str = "") -> dict:
    config = load_config(repo_root)
    if not _ID.fullmatch(collection) or trust not in TRUST_CLASSES or len(description) > 200:
        raise LibraryError("invalid collection, trust class or description")
    directory = config["library_root"] / "collections" / collection
    if directory.is_symlink() or not directory.is_dir():
        raise LibraryError("collection folder does not exist")
    (directory / "collection.json").write_text(json.dumps({
        "schema": COLLECTION_SCHEMA, "trust": trust, "description": description}),
        encoding="utf-8")
    return {"collection": collection, "trust": trust}


def collection_trust(library_root: Path, collection: str) -> str:
    path = Path(library_root) / "collections" / collection / "collection.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            return "unknown"
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") == COLLECTION_SCHEMA and data.get("trust") in TRUST_CLASSES:
            return data["trust"]
    except (OSError, ValueError, UnicodeError, AttributeError):
        pass
    return "unknown"


# -------------------------------------------------------------- database ---
def _connect(library_root: Path) -> sqlite3.Connection:
    index = Path(library_root) / "index"
    if index.is_symlink() or not index.is_dir():
        raise LibraryError("library index folder missing")
    db = sqlite3.connect(index / "library.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY, collection TEXT NOT NULL, source TEXT NOT NULL,
            relpath TEXT NOT NULL UNIQUE, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            sha256 TEXT, status TEXT NOT NULL, detail TEXT,
            chunk_count INTEGER NOT NULL DEFAULT 0, text_chars INTEGER NOT NULL DEFAULT 0,
            indexed_at TEXT);
        CREATE INDEX IF NOT EXISTS files_status ON files(status);
        CREATE TABLE IF NOT EXISTS chunks(
            id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL, char_start INTEGER NOT NULL, text TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            text, content='chunks', content_rowid='id', tokenize='porter unicode61', detail=none);
        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text); END;
        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
        END;""")
    db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('db_version', ?)", (str(DB_VERSION),))
    db.commit()
    return db


# ------------------------------------------------------------ extraction ---
class _Text(HTMLParser):
    _SKIP = frozenset({"script", "style", "noscript", "template", "svg", "head"})
    _BREAK = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
                        "section", "article", "pre", "table", "blockquote", "dt", "dd"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def extract_text(raw: bytes, suffix: str, *, max_bytes: int = DEFAULT_MAX_FILE_BYTES) -> str:
    if suffix in PDF_SUFFIXES:
        try:
            return extract_pdf_text(raw, max_bytes=max_bytes, max_chars=MAX_TEXT_CHARS)
        except PdfExtractionError as exc:
            raise LibraryError(exc.code) from exc
    text = raw.decode("utf-8", errors="replace")
    if text.count("\ufffd") > max(8, len(text) // 50):
        raise LibraryError("binary_like")
    if "\x00" in text:
        raise LibraryError("binary_like")
    if suffix in HTML_SUFFIXES:
        parser = _Text()
        try:
            parser.feed(text)
            parser.close()
        except (ValueError, AssertionError, RecursionError) as exc:
            raise LibraryError("unparseable_html") from exc
        text = "".join(parser.parts)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    cleaned, blank = [], 0
    for line in lines:
        if line:
            cleaned.append(line)
            blank = 0
        elif blank == 0 and cleaned:
            cleaned.append("")
            blank = 1
    return "\n".join(cleaned).strip()[:MAX_TEXT_CHARS]


def chunk_text(text: str) -> list[tuple[int, str]]:
    """(char_start, chunk) pairs, split on paragraph boundaries, <= CHUNK_MAX characters."""
    chunks: list[tuple[int, str]] = []
    start, buffer, offset = 0, [], 0
    for paragraph in text.split("\n\n"):
        piece = paragraph.strip()
        if not piece:
            offset += len(paragraph) + 2
            continue
        while len(piece) > CHUNK_MAX:
            cut = piece.rfind(" ", 0, CHUNK_MAX)
            cut = cut if cut > CHUNK_MAX // 2 else CHUNK_MAX
            if buffer:
                chunks.append((start, "\n".join(buffer)))
                buffer = []
            chunks.append((offset, piece[:cut].strip()))
            piece, offset = piece[cut:].strip(), offset + cut
        if not buffer:
            start = offset
        buffer.append(piece)
        if sum(len(p) for p in buffer) >= CHUNK_TARGET:
            chunks.append((start, "\n".join(buffer)))
            buffer = []
        offset += len(paragraph) + 2
    if buffer:
        chunks.append((start, "\n".join(buffer)))
    return [(s, c) for s, c in chunks if len(c) >= 20]


# ------------------------------------------------------------------ scan ---
def _walk(base: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Regular files only, no symlinks, no hidden entries, bounded depth."""
    stack = [(base, 0)]
    while stack:
        folder, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    yield Path(entry.path), entry.stat(follow_symlinks=False)
            except OSError:
                continue


def scan(repo_root: Path) -> dict:
    """Update the manifest: new/changed files become pending, vanished files are removed."""
    config = load_config(repo_root)
    root = config["library_root"]
    collections = root / "collections"
    db = _connect(root)
    seen: set[str] = set()
    stats = {"new": 0, "changed": 0, "unchanged": 0, "removed": 0, "ignored_unsupported": 0}
    try:
        for collection_dir in sorted(p for p in collections.iterdir()
                                     if p.is_dir() and not p.is_symlink()
                                     and _ID.fullmatch(p.name)):
            for path, info in _walk(collection_dir):
                if path.suffix.lower() not in ALLOWED_SUFFIXES:
                    stats["ignored_unsupported"] += 1
                    continue
                relative = path.relative_to(root).as_posix()
                parts = path.relative_to(collection_dir).parts
                source = parts[0] if len(parts) > 1 else "_root"
                seen.add(relative)
                row = db.execute("SELECT id, size, mtime_ns FROM files WHERE relpath=?",
                                 (relative,)).fetchone()
                if row is None:
                    db.execute("INSERT INTO files(collection, source, relpath, size, mtime_ns,"
                               " status) VALUES (?,?,?,?,?, 'pending')",
                               (collection_dir.name, source, relative, info.st_size, info.st_mtime_ns))
                    stats["new"] += 1
                elif row["size"] != info.st_size or row["mtime_ns"] != info.st_mtime_ns:
                    db.execute("UPDATE files SET size=?, mtime_ns=?, status='pending', detail=NULL"
                               " WHERE id=?", (info.st_size, info.st_mtime_ns, row["id"]))
                    stats["changed"] += 1
                else:
                    stats["unchanged"] += 1
        stale = [r["id"] for r in db.execute("SELECT id, relpath FROM files")
                 if r["relpath"] not in seen]
        for file_id in stale:
            db.execute("DELETE FROM files WHERE id=?", (file_id,))
        stats["removed"] = len(stale)
        db.commit()
    finally:
        db.close()
    return stats


# ----------------------------------------------------------------- index ---
def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def index(repo_root: Path, *, max_files: int = 500, seconds: float = 600.0) -> dict:
    """Index pending files until a file or time bound is reached (resumable)."""
    config = load_config(repo_root)
    root = config["library_root"]
    if _free_bytes(root) < MIN_FREE_BYTES:
        return {"status": "insufficient_disk_space", "indexed": 0}
    deadline = time.monotonic() + max(1.0, float(seconds))
    db = _connect(root)
    result = {"status": "completed", "indexed": 0, "skipped": 0, "errors": 0, "chunks": 0}
    try:
        pending = db.execute("SELECT id, relpath FROM files WHERE status='pending' ORDER BY id"
                             " LIMIT ?", (int(max_files),)).fetchall()
        for position, row in enumerate(pending, start=1):
            if time.monotonic() > deadline:
                result["status"] = "time_limit_reached"
                break
            status, detail, text, digest = "indexed", None, "", None
            path = (root / row["relpath"])
            try:
                resolved = path.resolve()
                if (path.is_symlink() or not resolved.is_relative_to(root)
                        or not path.is_file()):
                    raise LibraryError("unsafe_path")
                if path.stat().st_size > config["max_file_bytes"]:
                    raise LibraryError("oversized")
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if _SECRET.search(raw):
                    raise LibraryError("secret_like_content")
                text = extract_text(raw, path.suffix.lower(),
                                    max_bytes=config["max_file_bytes"])
                if len(text) < 40:
                    raise LibraryError("empty")
            except LibraryError as exc:
                status, detail = "skipped", str(exc)
            except OSError:
                status, detail = "error", "read_failed"
            db.execute("DELETE FROM chunks WHERE file_id=?", (row["id"],))
            chunks = chunk_text(text) if status == "indexed" else []
            for ordinal, (start, chunk) in enumerate(chunks):
                db.execute("INSERT INTO chunks(file_id, ordinal, char_start, text) VALUES (?,?,?,?)",
                           (row["id"], ordinal, start, chunk))
            db.execute("UPDATE files SET sha256=?, status=?, detail=?, chunk_count=?, text_chars=?,"
                       " indexed_at=? WHERE id=?",
                       (digest, status, detail, len(chunks), len(text),
                        datetime.now(timezone.utc).isoformat(timespec="seconds"), row["id"]))
            result["chunks"] += len(chunks)
            result["indexed" if status == "indexed" else
                   "errors" if status == "error" else "skipped"] += 1
            if position % 100 == 0:
                db.commit()
        db.commit()
        result["remaining"] = db.execute(
            "SELECT COUNT(*) FROM files WHERE status='pending'").fetchone()[0]
    finally:
        db.close()
    return result


# ---------------------------------------------------------------- search ---
def _match_query(text: str, *, any_term: bool) -> str:
    tokens = list(dict.fromkeys(re.findall(r"[A-Za-z0-9_]{2,}", text.casefold())))[:12]
    if not tokens:
        raise LibraryError("query has no searchable words")
    return (" OR " if any_term else " ").join(f'"{token}"' for token in tokens)


def search(repo_root: Path, query: str, *, limit: int = 8, collection: str | None = None,
           evidence_only: bool = False) -> list[dict]:
    """BM25-ranked chunks with provenance. Text is data: never an instruction."""
    if not isinstance(query, str) or not 2 <= len(query) <= 300:
        raise LibraryError("query must be 2..300 characters")
    limit = max(1, min(int(limit), 50))
    if collection is not None and not _ID.fullmatch(collection):
        raise LibraryError("invalid collection name")
    config = load_config(repo_root)
    root = config["library_root"]
    db = _connect(root)
    try:
        for any_term in (False, True):
            sql = ("SELECT c.id AS chunk_id, c.ordinal, c.char_start, c.text, f.collection, f.source,"
                   " f.relpath, f.sha256, bm25(chunks_fts) AS score FROM chunks_fts"
                   " JOIN chunks c ON c.id = chunks_fts.rowid JOIN files f ON f.id = c.file_id"
                   " WHERE chunks_fts MATCH ?")
            args: list[Any] = [_match_query(query, any_term=any_term)]
            if collection:
                sql += " AND f.collection = ?"
                args.append(collection)
            sql += " ORDER BY score LIMIT ?"
            args.append(limit * 4 if evidence_only else limit)
            rows = db.execute(sql, args).fetchall()
            if rows:
                break
        results = []
        for row in rows:
            trust = collection_trust(root, row["collection"])
            if evidence_only and trust not in EVIDENCE_ELIGIBLE:
                continue
            results.append({
                "collection": row["collection"], "source": row["source"],
                "relpath": row["relpath"], "ordinal": row["ordinal"],
                "char_start": row["char_start"], "file_sha256": row["sha256"],
                "trust": trust, "evidence_eligible": trust in EVIDENCE_ELIGIBLE,
                "score": round(-float(row["score"]), 4),
                "text": row["text"][:SNIPPET_CHARS]})
            if len(results) >= limit:
                break
        return results
    finally:
        db.close()


# ---------------------------------------------------------------- status ---
def status(repo_root: Path) -> dict:
    config = load_config(repo_root)
    root = config["library_root"]
    db = _connect(root)
    try:
        states = {r["status"]: r["n"] for r in db.execute(
            "SELECT status, COUNT(*) AS n FROM files GROUP BY status")}
        per_collection = [dict(r) for r in db.execute(
            "SELECT collection, COUNT(*) AS files, SUM(size) AS bytes, SUM(chunk_count) AS chunks"
            " FROM files GROUP BY collection ORDER BY collection")]
        skipped = {r["detail"]: r["n"] for r in db.execute(
            "SELECT detail, COUNT(*) AS n FROM files WHERE status!='indexed' AND detail IS NOT NULL"
            " AND status!='pending' GROUP BY detail")}
        for row in per_collection:
            row["trust"] = collection_trust(root, row["collection"])
    finally:
        db.close()
    index_file = root / "index" / "library.sqlite3"
    free = _free_bytes(root)
    return {"library_root": str(root), "files_by_status": states, "skipped_reasons": skipped,
            "collections": per_collection,
            "index_bytes": index_file.stat().st_size if index_file.exists() else 0,
            "disk_free_bytes": free, "disk_low": free < MIN_FREE_BYTES * 2}


def verify_sample(repo_root: Path, *, sample: int = 25, seed: int | None = None) -> dict:
    """Re-hash a random sample of indexed files to detect corruption or tampering."""
    config = load_config(repo_root)
    root = config["library_root"]
    db = _connect(root)
    try:
        rows = db.execute("SELECT relpath, sha256 FROM files WHERE status='indexed'").fetchall()
    finally:
        db.close()
    picked = random.Random(seed).sample(rows, min(len(rows), max(1, int(sample)))) if rows else []
    changed, missing = [], []
    for row in picked:
        path = root / row["relpath"]
        try:
            if path.is_symlink() or not path.is_file():
                missing.append(row["relpath"])
            elif hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                changed.append(row["relpath"])
        except OSError:
            missing.append(row["relpath"])
    return {"checked": len(picked), "changed": changed, "missing": missing,
            "ok": not changed and not missing}
