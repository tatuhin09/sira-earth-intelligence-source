"""Local multilingual profiling plus evidence-gated learned query expansion."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import unicodedata
from typing import Mapping

MAX_TEXT_CHARS = 8000
BANGLISH_MARKERS = frozenset({
    "ami","amake","amk","amar","amr","amader","tumi","tmr","tomar","apni","apnar",
    "se","take","tar","ki","kno","keno","kivabe","kemne","kothay","kothy",
    "akhon","ekhon","pore","age","abar","abr","ache","ase","nai","nei","hobe",
    "hoi","hoy","hoice","hoise","korbo","koro","kore","korci","korchi","korte",
    "korbe","dibo","diba","dao","daw","bolo","bolba","bolbe","buj","bujhe",
    "bujbe","bujte","sikh","sikhe","sikhbe","sikhte","lagbe","dorkar","jonno",
    "jodi","tahole","tayna","onek","bhalo","valo","khoti","somossa","kichu",
    "sob","shob","eita","eta","oita","parbe","parbo","pari","pare","hee","na",
})

ENGLISH_MARKERS = frozenset({
    "the","a","an","is","are","was","were","be","to","of","for","in","on","with",
    "this","that","what","why","how","can","could","should","will","would","please",
    "use","run","code","test","research","memory","language","learn","learning","and",
})


def normalize_unicode_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("text must be a string")
    value = " ".join(unicodedata.normalize("NFKC", value).split())
    if not value or len(value) > MAX_TEXT_CHARS:
        raise ValueError("text must contain 1..8000 normalized characters")
    value.encode("utf-8")
    return value


def _word_char(char: str) -> bool:
    category = unicodedata.category(char)
    return bool(category and category[0] in {"L", "N", "M"})


def _unicode_word_tokens_raw(value: str) -> tuple[str, ...]:
    tokens: list[str] = []
    current: list[str] = []
    for index, char in enumerate(value):
        if _word_char(char):
            current.append(char)
            continue
        if (
            char in {"'", "’", "-"}
            and current
            and index + 1 < len(value)
            and _word_char(value[index + 1])
        ):
            current.append(char)
            continue
        if current:
            tokens.append("".join(current).casefold())
            current = []
    if current:
        tokens.append("".join(current).casefold())
    return tuple(tokens)


def unicode_tokens(value: str) -> tuple[str, ...]:
    value = normalize_unicode_text(value)
    return _unicode_word_tokens_raw(value)


def _script_counts(text: str) -> dict[str, int]:
    counts = {name: 0 for name in (
        "bengali","latin","arabic","devanagari","han","hiragana","katakana",
        "hangul","cyrillic","other_letters"
    )}
    for char in text:
        if not char.isalpha():
            continue
        code = ord(char)
        name = unicodedata.name(char, "")
        if 0x0980 <= code <= 0x09FF:
            counts["bengali"] += 1
        elif 0x0900 <= code <= 0x097F:
            counts["devanagari"] += 1
        elif 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F or 0x08A0 <= code <= 0x08FF:
            counts["arabic"] += 1
        elif 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
            counts["han"] += 1
        elif 0x3040 <= code <= 0x309F:
            counts["hiragana"] += 1
        elif 0x30A0 <= code <= 0x30FF:
            counts["katakana"] += 1
        elif 0xAC00 <= code <= 0xD7AF:
            counts["hangul"] += 1
        elif "LATIN" in name:
            counts["latin"] += 1
        elif "CYRILLIC" in name:
            counts["cyrillic"] += 1
        else:
            counts["other_letters"] += 1
    return counts


def _latin_tokens(tokens: tuple[str, ...]) -> tuple[str, ...]:
    rows = []
    for token in tokens:
        letters = [c for c in token if c.isalpha()]
        if letters and all("LATIN" in unicodedata.name(c, "") for c in letters):
            rows.append(token)
    return tuple(rows)


def _scores(tokens: tuple[str, ...]) -> tuple[float, tuple[str, ...], float]:
    latin = _latin_tokens(tokens)
    if not latin:
        return 0.0, (), 0.0
    banglish_hits = tuple(dict.fromkeys(t for t in latin if t in BANGLISH_MARKERS))
    banglish = min(1.0, len(banglish_hits) / max(3, min(12, len(latin))))
    english_hits = sum(t in ENGLISH_MARKERS for t in latin)
    english = min(1.0, english_hits / max(3, min(10, len(latin))))
    return round(banglish, 4), banglish_hits, round(english, 4)


def _classify(counts: Mapping[str, int], banglish: float, hits: tuple[str, ...], english: float):
    if counts["bengali"] and counts["latin"]:
        return "bn-en-mixed", 0.94, True
    if counts["bengali"]:
        return "bn", 0.98, True
    if counts["hiragana"] or counts["katakana"]:
        return "ja-script", 0.96, True
    if counts["hangul"]:
        return "ko-script", 0.96, True
    if counts["han"]:
        return "zh-or-cjk", 0.88, True
    if counts["arabic"]:
        return "ar-script", 0.90, True
    if counts["devanagari"]:
        return "hi-or-devanagari", 0.88, True
    if counts["cyrillic"]:
        return "und-Cyrl", 0.84, True
    if counts["latin"]:
        if len(hits) >= 2 and banglish >= 0.16:
            return "bn-Latn-banglish", min(0.97, 0.70 + banglish * 0.25), True
        if english >= 0.18:
            return "en", min(0.96, 0.72 + english * 0.25), False
        return "und-Latn", 0.58, True
    return "und", 0.35, True


def _learned_gloss(normalized: str, lexicon: Mapping[str, str]):
    if not lexicon:
        return None, ()
    parts = re.split(r"(\s+|[^\w'’\-]+)", normalized, flags=re.UNICODE)
    output, hits = [], []
    for part in parts:
        mapped = lexicon.get(part.casefold())
        if mapped:
            output.append(mapped)
            hits.append(part.casefold())
        else:
            output.append(part)
    gloss = " ".join("".join(output).split())
    if not hits or gloss.casefold() == normalized.casefold():
        return None, ()
    return gloss, tuple(dict.fromkeys(hits))


@dataclass(frozen=True, slots=True)
class LanguageProfile:
    original_text: str
    normalized_text: str
    language_label: str
    script_counts: dict[str, int]
    token_count: int
    banglish_score: float
    banglish_markers: tuple[str, ...]
    local_confidence: float
    needs_semantic_teacher: bool
    learned_gloss: str | None
    learned_mapping_hits: tuple[str, ...]
    research_seed_queries: tuple[str, ...]
    original_preserved: bool = True

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["banglish_markers"] = list(self.banglish_markers)
        value["learned_mapping_hits"] = list(self.learned_mapping_hits)
        value["research_seed_queries"] = list(self.research_seed_queries)
        return value


def analyze_language(text: str, *, learned_lexicon: Mapping[str, str] | None = None) -> LanguageProfile:
    normalized = normalize_unicode_text(text)
    tokens = unicode_tokens(normalized)
    counts = _script_counts(normalized)
    banglish, hits, english = _scores(tokens)
    label, confidence, teacher = _classify(counts, banglish, hits, english)
    gloss, mapping_hits = _learned_gloss(normalized, learned_lexicon or {})
    seeds = [normalized]
    if gloss and gloss.casefold() != normalized.casefold():
        seeds.append(gloss)
    return LanguageProfile(
        original_text=text,
        normalized_text=normalized,
        language_label=label,
        script_counts=counts,
        token_count=len(tokens),
        banglish_score=banglish,
        banglish_markers=hits,
        local_confidence=round(confidence, 4),
        needs_semantic_teacher=teacher,
        learned_gloss=gloss,
        learned_mapping_hits=mapping_hits,
        research_seed_queries=tuple(dict.fromkeys(seeds)),
    )


def analyze_language_with_store(root: Path, text: str) -> LanguageProfile:
    from .learning_consolidation import LearningConsolidationStore
    return analyze_language(
        text,
        learned_lexicon=LearningConsolidationStore(root).active_lexicon(),
    )
