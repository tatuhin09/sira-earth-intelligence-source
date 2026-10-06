"""Bounded, explicitly scoped documentation reading for a learning goal.

This source adapter does not verify claims or write factual knowledge.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from .learning_goals import LearningGoalStore
from .models import utc_now
from .pdf_extraction import extract_pdf_text
from .providers.http_json import open_request
from .reading import Document
from .reports import evidence_quotes
from .storage import write_json


_PYTHON_TESTING = (
    "https://docs.python.org/3/library/unittest.html",
    "https://docs.pytest.org/en/stable/getting-started.html",
)
_MAX_HTML_BYTES = 1024 * 1024
_MAX_PDF_BYTES = 8 * 1024 * 1024
_MAX_TEXT_CHARS = 80_000
_SKIP = frozenset({"script", "style", "nav", "footer", "header", "aside", "form", "svg"})
_BLOCKS = frozenset({"p", "li", "h1", "h2", "h3", "h4", "pre", "blockquote"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})


class _MainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.main_depth = 0
        self.skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if not self.main_depth and (tag == "main" or dict(attrs).get("role") == "main"):
            self.main_depth = 1
        elif self.main_depth and tag not in _VOID:
            self.main_depth += 1
        if self.main_depth:
            if self.skip_depth:
                if tag not in _VOID:
                    self.skip_depth += 1
            elif tag in _SKIP:
                self.skip_depth = 1
            elif tag in _BLOCKS:
                self.parts.append("\n")

    def handle_endtag(self, tag):
        if not self.main_depth:
            return
        if self.skip_depth:
            self.skip_depth -= 1
        elif tag in _BLOCKS:
            self.parts.append("\n")
        if tag not in _VOID:
            self.main_depth -= 1

    def handle_data(self, data):
        if self.main_depth and not self.skip_depth:
            self.parts.append(data)


def _extract_text(raw: bytes) -> str:
    try:
        parser = _MainText()
        parser.feed(raw.decode("utf-8"))
        parser.close()
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Invalid documentation HTML") from exc
    text = "\n".join(
        line for fragment in " ".join(parser.parts).splitlines()
        if (line := " ".join(fragment.split()))
    )[:_MAX_TEXT_CHARS]
    if len(text) < 40:
        raise ValueError("No readable main documentation content")
    return text


def _extract_public_document_text(raw: bytes, content_type: str) -> str:
    """Read a bounded HTML or text-PDF response for any public research worker."""
    if not isinstance(raw, bytes) or not raw:
        raise ValueError("Empty public document")
    if content_type == "application/pdf" or raw.startswith(b"%PDF-"):
        return extract_pdf_text(raw, max_bytes=_MAX_PDF_BYTES,
                                max_chars=_MAX_TEXT_CHARS)
    if content_type != "text/html" or len(raw) > _MAX_HTML_BYTES:
        raise ValueError("Unsupported public document type or size")
    return _extract_text(raw)


def _fetch_html(url: str) -> tuple[str, str, bytes]:
    request = Request(url, method="GET", headers={
        "Accept": "text/html, application/pdf", "User-Agent": "SIRA/1.8 public research",
    })
    try:
        with open_request(request, 10) as response:
            actual_url = response.geturl()
            content_type = response.headers.get_content_type()
            limit = (_MAX_PDF_BYTES if content_type == "application/pdf"
                     or urlsplit(actual_url).path.lower().endswith(".pdf")
                     else _MAX_HTML_BYTES)
            raw = response.read(limit + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ValueError("Official documentation unavailable") from exc
    return actual_url, content_type, raw


def _validate_saved(saved: object, goal_id: str) -> dict:
    if (not isinstance(saved, dict)
            or saved.get("schema") != "sira.learning_goal_document_evidence.v1"
            or saved.get("learning_goal_id") != goal_id
            or saved.get("status") not in {"quotes_need_claim_verification", "insufficient_source_text"}
            or saved.get("verified_claims_recorded") is not False
            or saved.get("promotion_performed") is not False
            or saved.get("metered_model_requests") != 0
            or saved.get("source_count") != len(_PYTHON_TESTING)
            or saved.get("distinct_host_count") != len(_PYTHON_TESTING)
            or not isinstance(saved.get("sources"), list)
            or len(saved["sources"]) != len(_PYTHON_TESTING)):
        raise ValueError("Saved documentation evidence is invalid")
    for source, url in zip(saved["sources"], _PYTHON_TESTING):
        if (not isinstance(source, dict) or source.get("url") != url
                or source.get("host") != urlsplit(url).hostname
                or source.get("verified") is not False):
            raise ValueError("Saved documentation source mismatch")
        text = source.get("text")
        if (not isinstance(text, str) or len(text) > _MAX_TEXT_CHARS
                or hashlib.sha256(text.encode("utf-8")).hexdigest() != source.get("content_sha256")
                or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("raw_sha256")))
                or not isinstance(source.get("quotes"), list)):
            raise ValueError("Saved documentation text has changed")
        for quote in source["quotes"]:
            if (not isinstance(quote, dict) or quote.get("verified") is not False
                    or type(quote.get("start")) is not int or type(quote.get("end")) is not int
                    or text[quote["start"]:quote["end"]] != quote.get("quote")
                    or quote.get("content_sha256") != source["content_sha256"]):
                raise ValueError("Saved quote integrity failed")
    return saved


def collect_official_learning_sources(
    root: Path, goal_id: str, *,
    fetcher: Callable[[str], tuple[str, str, bytes]] = _fetch_html,
) -> dict:
    """One bounded fetch per fixed host; reuse a locally validated artifact."""
    root = Path(root).resolve()
    goals = LearningGoalStore(root)
    goal = goals.get(goal_id)
    if goal["topic"].casefold() != "python software testing":
        raise ValueError("No official source plan for this learning goal yet")
    def allowed() -> bool:
        current = goals.get(goal_id)
        return current["status"] == "active" and current["public_research_allowed"] is True
    if not allowed():
        raise ValueError("Public learning research is disabled")
    directory = root / "memory" / "learning_goal_documents"
    artifact = directory / f"{goal_id}_python_testing_v1.json"
    if (directory.parent.is_symlink() or directory.is_symlink()
            or artifact.is_symlink()):
        raise ValueError("Documentation evidence path is unsafe")
    if artifact.exists():
        if not artifact.is_file() or artifact.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Saved documentation evidence is unsafe")
        try:
            saved = _validate_saved(json.loads(artifact.read_text(encoding="utf-8")), goal_id)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Saved documentation evidence cannot be read") from exc
        return {**saved, "api_requests": 0, "cache_replay": True, "artifact": str(artifact)}

    sources = []
    for url in _PYTHON_TESTING:
        if not allowed():
            raise ValueError("Public learning research was revoked")
        actual, content_type, raw = fetcher(url)
        if (actual != url or not isinstance(raw, bytes) or not raw
                or len(raw) > (_MAX_PDF_BYTES if content_type == "application/pdf"
                               or raw.startswith(b"%PDF-") else _MAX_HTML_BYTES)):
            raise ValueError("Unexpected official documentation response")
        text = _extract_public_document_text(raw, content_type)
        doc = Document(url, text, utc_now())
        quotes = [
            {**quote, "verified": False, "content_origin": "official_documentation_page"}
            for quote in evidence_quotes(goal["topic"], "S" + str(len(sources) + 1), doc)
        ]
        for quote in quotes:
            if doc.text[quote["start"]:quote["end"]] != quote["quote"]:
                raise ValueError("Quote integrity failure")
        sources.append({
            "url": url, "host": urlsplit(url).hostname, "retrieved_at": doc.retrieved_at,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "content_sha256": doc.content_sha256, "text": text, "quotes": quotes,
            "verified": False,
        })
    report = {
        "schema": "sira.learning_goal_document_evidence.v1",
        "status": "quotes_need_claim_verification" if all(row["quotes"] for row in sources) else "insufficient_source_text",
        "learning_goal_id": goal_id, "topic": goal["topic"],
        "source_count": len(sources),
        "distinct_host_count": len({row["host"] for row in sources}),
        "sources": sources, "verified_claims_recorded": False,
        "api_requests": len(_PYTHON_TESTING), "metered_model_requests": 0,
        "promotion_performed": False, "cache_replay": False,
    }
    if not allowed():
        raise ValueError("Public learning research was revoked")
    write_json(artifact, report)
    return {**report, "artifact": str(artifact)}
