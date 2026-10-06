"""Re-select explanatory quotes from locally cached official documentation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .learning_goal_official_sources import _PYTHON_TESTING, _validate_saved
from .learning_goals import LearningGoalStore
from .storage import write_json


_TEST = re.compile(r"\b(?:tests?|testing|testcase|assertions?|automation)\b", re.I)
_EXPLAINS = re.compile(
    r"\b(?:supports?|provides?|discovers?|checks?|verif(?:y|ies)|allows?|uses?|runs?|"
    r"requires?|represents?|aggregates?)\b", re.I
)
_FRAMEWORK = {
    "docs.python.org": re.compile(r"\b(?:unittest|test case|test fixture|test suite)\b", re.I),
    "docs.pytest.org": re.compile(r"\bpytest\b", re.I),
}
_BOILERPLATE = re.compile(r"\b(?:see also|testing tools taxonomy|an extensive list)\b", re.I)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def _sentence_end(text: str, start: int) -> int | None:
    """Preserve exact offsets while ignoring a source page's formatting line breaks."""
    window = text[start:start + 500]
    boundaries = list(_SENTENCE_END.finditer(window))
    if not boundaries:
        return None
    first = boundaries[0].end()
    # A short first sentence often introduces the explanation in the second.
    chosen = boundaries[1].end() if first < 60 and len(boundaries) > 1 else first
    return start + chosen


def _explanatory_quotes(source: dict, source_id: str) -> list[dict]:
    text = source["text"]
    host = source["host"]
    ranked = []
    for line in re.finditer(r"[^\r\n]+", text):
        paragraph = line.group().strip()
        first_line = paragraph[:500]
        if (len(first_line) < 70 or not _TEST.search(first_line) or not _EXPLAINS.search(first_line)
                or not _FRAMEWORK[host].search(first_line) or _BOILERPLATE.search(first_line)):
            continue
        start = line.start() + len(line.group()) - len(line.group().lstrip())
        end = _sentence_end(text, start)
        if end is None:
            continue
        snippet = text[start:end]
        score = (3 * bool(re.search(r"\b(?:test automation|test discovery|discovers all tests|test case)\b", snippet, re.I))
                 + 2 * bool(re.search(r"\b(?:unittest|pytest)\b", snippet, re.I))
                 + min(len(snippet), 300) / 300)
        ranked.append((-score, start, end, snippet))
    ranked.sort()
    return [
        {"source_id": source_id, "url": source["url"], "start": start, "end": end,
         "quote": snippet, "content_sha256": source["content_sha256"],
         "selection_method": "explanatory_sentence_v3", "verified": False}
        for _, start, end, snippet in ranked[:2]
    ]


def replay_official_learning_quotes(root: Path, goal_id: str) -> dict:
    """Offline replay only; never fetch pages or promote a source assertion to fact."""
    root = Path(root).resolve()
    goal = LearningGoalStore(root).get(goal_id)
    if goal["topic"].casefold() != "python software testing":
        raise ValueError("No official documentation source plan for this topic")
    directory = root / "memory" / "learning_goal_documents"
    source_path = directory / f"{goal_id}_python_testing_v1.json"
    if (directory.parent.is_symlink() or directory.is_symlink() or source_path.is_symlink()
            or not source_path.is_file() or source_path.stat().st_size > 2 * 1024 * 1024):
        raise ValueError("Official documentation evidence missing or unsafe")
    source_bytes = source_path.read_bytes()
    try:
        original = _validate_saved(json.loads(source_bytes), goal_id)
    except (UnicodeError, json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise ValueError("Official documentation evidence is invalid") from exc
    if (original.get("topic") != goal["topic"]
            or [s["url"] for s in original["sources"]] != list(_PYTHON_TESTING)):
        raise ValueError("Documentation does not match the learning goal")
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    output_dir = root / "memory" / "learning_goal_evidence"
    output = output_dir / f"{goal_id}_{source_sha}_official_relevant_v3.json"
    if output_dir.is_symlink() or output.is_symlink():
        raise ValueError("Official quote evidence path is unsafe")

    quotes = [quote for index, source in enumerate(original["sources"], 1)
              for quote in _explanatory_quotes(source, f"S{index}")]
    hosts = {urlsplit(q["url"]).hostname for q in quotes}
    report = {
        "schema": "sira.learning_goal_official_relevance.v3",
        "status": ("relevant_quotes_need_claim_verification" if len(hosts) == 2 else
                   "insufficient_relevant_documentation"),
        "learning_goal_id": goal_id, "topic": goal["topic"],
        "source_report_sha256": source_sha, "source_count": 2,
        "distinct_quote_host_count": len(hosts), "quote_count": len(quotes),
        "quotes": quotes, "verified_claims_recorded": False,
        "api_requests": 0, "metered_model_requests": 0, "promotion_performed": False,
    }
    if output.exists():
        if not output.is_file() or output.stat().st_size > 100_000:
            raise ValueError("Existing official quote evidence is unsafe")
        try:
            saved = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Existing official quote evidence cannot be read") from exc
        if saved != report:
            raise ValueError("Existing official quote evidence does not match source")
    else:
        write_json(output, report)
    return {**report, "artifact": str(output)}
