"""Bounded local teaching response from fresh evidence-gated knowledge."""
from __future__ import annotations

from pathlib import Path
import re

from .knowledge_consolidation import KnowledgeConsolidationStore


_LEARNING_CUES = (
    "teach me", "what did you learn", "what have you learned", "explain",
    "শেখাও", "শিখেছ", "শিখেছেন", "ব্যাখ্যা কর",
)
_MEMORY_QUESTION_CUES = ("what did you learn", "what have you learned", "কি শিখেছ", "কী শিখেছ")
_IGNORE = frozenset({
    "about", "and", "did", "do", "explain", "have", "learn", "learned",
    "me", "please", "teach", "the", "what", "you", "your", "of", "for",
})


def _terms(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", text.casefold())) - _IGNORE
    return {"test" if token in {"tests", "testing"} else token for token in tokens}


def verified_knowledge_reply(root: Path, message: str) -> tuple[str, str] | None:
    """Return one relevant, cited fact; leave other chat requests untouched."""
    if not isinstance(message, str) or not any(cue in message.casefold() for cue in _LEARNING_CUES):
        return None
    memory_question = any(cue in message.casefold() for cue in _MEMORY_QUESTION_CUES)
    unavailable = (
        "I have no matching verified local claim for that question yet. "
        "You can ask for explicit research or a narrower topic.",
        "local_knowledge_unavailable",
    ) if memory_question else None
    query_terms = _terms(message)
    if len(query_terms) < 2:
        return unavailable
    try:
        store = KnowledgeConsolidationStore(root)
        for row in store.search(message, limit=5):
            matched = query_terms & _terms(str(row["claim_text"]))
            if len(matched) < 2 or len(matched) / len(query_terms) < .5:
                continue
            if int(row["host_count"]) < 2:
                continue
            urls = store.verified_source_urls(str(row["knowledge_key"]), limit=4)
            if len({url.split("/")[2] for url in urls}) < 2:
                continue
            sources = "\n".join("- " + url for url in urls)
            return ((
                f"One verified claim relevant to your question: {row['claim_text']}\n"
                f"Sources:\n{sources}\n"
                "This one claim does not cover the whole topic. Further research "
                "and verification are needed for a broader explanation."
            ), "local_verified_knowledge")
    except (OSError, ValueError, RuntimeError):
        return unavailable
    return unavailable
