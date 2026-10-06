"""Quote-grounded claim verification for learning goals (model-proposed, locally gated).

Independent sources rarely repeat one exact sentence, so the exact-sentence rule
verifies almost nothing. This module lets a model PROPOSE an atomic claim that
cites sentence ids from at least two different hosts. The model is never trusted:
every cited sentence is a verbatim sentence from a document SIRA fetched itself,
and a deterministic local gate checks term coverage by each cited source, numbers,
negation, topic relevance, unsafe content and conflicting passages. Accepted claims
enter the existing evidence-gated knowledge store at the lowest verified tier
(confidence 0.80, verifier_kind ``grounded_claim_local_gate_v1``) so that retrieval
can tell them apart from exact-text verification (0.85).

Owner policy (default: disabled) bounds the daily model requests. No paid spending,
authority, promotion or skill activation is ever involved.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from .knowledge_consolidation import KnowledgeConsolidationStore, auto_key, stable_evidence_id
from .learning_goal_general_learning import _statements, _topic_terms, _words
from .learning_goals import LearningGoalStore
from .models import ProviderError, utc_now
from .storage import write_json
from .synthesis import PROPOSAL_SCHEMA, _validate_proposal

POLICY_SCHEMA = "sira.learning_goal_grounded_policy.v1"
EVIDENCE_SCHEMA = "sira.learning_goal_grounded_evidence.v1"
VERIFIER_KIND = "grounded_claim_local_gate_v1"
PROMPT_VERSION = "grounded-claims-v1"
CONFIDENCE = 0.80
MAX_DAILY_CAP = 200
MAX_DOCUMENTS = 3
MAX_PASSAGES_PER_DOCUMENT = 10
MAX_ACCEPTED_CLAIMS = 2
MAX_SEEN = 200
_STALE_AFTER_DAYS = 365

_STOP = frozenset(
    "the and for with that this from are was were been has have had but can its into than "
    "then they them their there these those which will would should could may might also "
    "such only other more most some any all each both not you your our can how what when "
    "where who why use used using one two".split())
_NEGATION = re.compile(r"(?i)\b(?:not|no|never|cannot|without|neither|nor|none)\b|n['\u2019]t\b")
_UNSAFE = re.compile(r"(?i)(?:https?://|www\.|`|\$\(|\bsudo\b|\brm\s+-|\bcurl\b|\bwget\b|<script|\bexec\()")


# ---------------------------------------------------------------- policy ---
def _directory(root: Path) -> Path:
    return Path(root) / "memory" / "learning_goal_grounded"


def load_policy(root: Path) -> dict:
    """Owner policy outside candidate-copyable code. Absent file => disabled."""
    policy = {"enabled": False, "daily_model_requests": 0, "valid": True, "source": "default"}
    path = _directory(root) / "policy.json"
    if not path.exists() and not path.is_symlink():
        return policy
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            raise ValueError("unsafe policy")
        data = json.loads(path.read_text(encoding="utf-8"))
        cap = data.get("daily_model_requests")
        if (not isinstance(data, dict) or data.get("schema") != POLICY_SCHEMA
                or set(data) != {"schema", "enabled", "daily_model_requests"}
                or type(data["enabled"]) is not bool or type(cap) is not int
                or not 0 <= cap <= MAX_DAILY_CAP):
            raise ValueError("invalid policy")
    except (OSError, ValueError, UnicodeError, TypeError):
        return {"enabled": False, "daily_model_requests": 0, "valid": False,
                "source": "malformed"}
    return {"enabled": data["enabled"], "daily_model_requests": cap, "valid": True,
            "source": "owner_policy"}


def write_policy(root: Path, *, enabled: bool, daily_model_requests: int) -> dict:
    if type(enabled) is not bool or type(daily_model_requests) is not int \
            or not 0 <= daily_model_requests <= MAX_DAILY_CAP:
        raise ValueError(f"daily_model_requests must be 0..{MAX_DAILY_CAP}")
    directory = _directory(root)
    if directory.is_symlink() or Path(root, "memory").is_symlink():
        raise ValueError("unsafe policy directory")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "policy.json"
    temporary = directory / f".policy.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps({"schema": POLICY_SCHEMA, "enabled": enabled,
                                     "daily_model_requests": daily_model_requests}),
                         encoding="utf-8")
    os.replace(temporary, path)
    return load_policy(root)


def _usage_path(root: Path) -> Path:
    return _directory(root) / "usage.json"


def _load_usage(root: Path, day: str) -> dict:
    try:
        data = json.loads(_usage_path(root).read_text(encoding="utf-8"))
        if (isinstance(data, dict) and isinstance(data.get("seen"), list)
                and type(data.get("count")) is int and data.get("day") == day):
            return {"day": day, "count": max(0, data["count"]),
                    "seen": [x for x in data["seen"] if isinstance(x, str)][-MAX_SEEN:]}
        if isinstance(data, dict) and isinstance(data.get("seen"), list):
            return {"day": day, "count": 0,
                    "seen": [x for x in data["seen"] if isinstance(x, str)][-MAX_SEEN:]}
    except (OSError, ValueError, UnicodeError, TypeError):
        pass
    return {"day": day, "count": 0, "seen": []}


def _save_usage(root: Path, usage: Mapping) -> None:
    directory = _directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".usage.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps(dict(usage)), encoding="utf-8")
    os.replace(temporary, _usage_path(root))


def default_claim_model(root: Path):
    """The configured Gemini model when the owner policy enables grounded claims."""
    policy = load_policy(root)
    if not (policy["valid"] and policy["enabled"] and policy["daily_model_requests"] > 0):
        return None
    try:
        from .config import load_key
        from .providers.gemini import GeminiModel
        return GeminiModel(load_key(Path(root), "GEMINI_API_KEY"))
    except (ValueError, OSError):
        return None


# -------------------------------------------------------------- passages ---
def _terms(text: str) -> set[str]:
    return {w for w in _words(text)
            if len(w) > 2 and w not in _STOP and not any(c.isdigit() for c in w)}


def _numbers(text: str) -> set[str]:
    return {w for w in _words(text) if any(c.isdigit() for c in w)}


def _negated(text: str) -> bool:
    return _NEGATION.search(text) is not None


def _jaccard(a: set, b: set) -> float:
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _relevant_sentences(text: str, focus_terms: set[str]) -> dict[str, str]:
    """Complete verbatim sentences sharing at least one focus term.

    Looser than the exact-sentence rule (two terms) because the claim gate, not passage
    selection, decides acceptance: it requires the claim itself to be on topic and to be
    covered by sentences from two hosts.
    """
    found: dict[str, str] = {}
    if not focus_terms:
        return found
    for match in re.finditer(r"[^.!?\n]{40,360}[.!?](?=\s|$)", text):
        sentence = " ".join(match.group().split())
        if not 7 <= len(sentence.split()) <= 55:
            continue
        if not set(_words(sentence)) & focus_terms:
            continue
        found.setdefault(" ".join(sentence.casefold().split()), sentence)
        if len(found) >= 500:
            break
    return found


def select_passages(documents: Sequence[Mapping], focus: str) -> list[dict]:
    """Verbatim, topic-relevant sentences from fetched documents with stable ids."""
    focus_terms = _topic_terms(focus)
    passages: list[dict] = []
    for source_index, doc in enumerate(list(documents)[:MAX_DOCUMENTS], start=1):
        text, url = doc.get("text"), doc.get("url")
        if not isinstance(text, str) or not isinstance(url, str):
            continue
        host = doc.get("host") or urlsplit(url).hostname
        if not isinstance(host, str) or not host:
            continue
        normalized = " ".join(text.split())
        scored = []
        for sentence in _relevant_sentences(text, focus_terms).values():
            if sentence not in normalized:
                continue
            hits = len(set(_words(sentence)) & focus_terms)
            scored.append((-hits, len(sentence), sentence))
        for _, _, sentence in sorted(scored)[:MAX_PASSAGES_PER_DOCUMENT]:
            if len(passages) >= 90:
                break
            passages.append({
                "id": f"E{len(passages) + 1}", "source_id": f"S{source_index}",
                "host": host.lower(), "url": url, "text": sentence,
                "content_sha256": doc.get("content_sha256") or hashlib.sha256(
                    text.encode("utf-8")).hexdigest(),
                "retrieved_at": doc.get("retrieved_at") or utc_now()})
    return passages


# ------------------------------------------------------------------ gate ---
def gate_claim(claim: Mapping, passages: Sequence[Mapping], focus: str) -> dict:
    """Deterministic acceptance gate. Returns accepted flag, reasons and metrics."""
    by_id = {p["id"]: p for p in passages}
    text = claim["text"].strip()
    reasons: list[str] = []
    support_ids, opposing_ids = claim["support_passage_ids"], claim["opposing_passage_ids"]
    if opposing_ids:
        reasons.append("proposal_has_opposition")
    if any(x not in by_id for x in support_ids + opposing_ids):
        reasons.append("proposal_unknown_passage")
    words = text.split()
    if not 20 <= len(text) <= 300 or not 5 <= len(words) <= 45:
        reasons.append("claim_length_out_of_bounds")
    if _UNSAFE.search(text):
        reasons.append("unsafe_claim_content")
    claim_terms = _terms(text)
    if len(claim_terms) < 3:
        reasons.append("claim_too_vague")
    focus_terms = _topic_terms(focus)
    if focus_terms and len(claim_terms & focus_terms) < min(2, len(focus_terms)):
        reasons.append("claim_off_topic")
    cited = [by_id[x] for x in dict.fromkeys(support_ids) if x in by_id]
    claim_negated, claim_numbers = _negated(text), _numbers(text)
    metrics: dict[str, Any] = {"claim_terms": len(claim_terms), "coverage": {}}
    supporting = []
    if claim_terms:
        for passage in cited:
            coverage = len(claim_terms & _terms(passage["text"])) / len(claim_terms)
            metrics["coverage"][passage["id"]] = round(coverage, 3)
            if coverage >= 0.5:
                supporting.append(passage)
    hosts = {p["host"] for p in supporting}
    if len(hosts) < 2:
        reasons.append("fewer_than_two_independent_supporting_hosts")
    if supporting and claim_terms:
        union = set().union(*(_terms(p["text"]) for p in supporting))
        union_coverage = len(claim_terms & union) / len(claim_terms)
        metrics["union_coverage"] = round(union_coverage, 3)
        if union_coverage < 0.8:
            reasons.append("claim_terms_not_covered_by_sources")
    for number in claim_numbers:
        if sum(number in _numbers(p["text"]) for p in supporting) < 2:
            reasons.append("number_not_in_two_sources")
            break
    if any(_negated(p["text"]) != claim_negated for p in supporting):
        reasons.append("polarity_mismatch")
    if not reasons:
        cited_ids = {p["id"] for p in supporting}
        for other in passages:
            if other["id"] in cited_ids:
                continue
            if (_jaccard(claim_terms, _terms(other["text"])) >= 0.5
                    and _negated(other["text"]) != claim_negated):
                reasons.append("conflicting_passage")
                break
    return {"accepted": not reasons, "reasons": reasons, "metrics": metrics,
            "supporting_ids": [p["id"] for p in supporting]}


# ---------------------------------------------------------- orchestration ---
def _input_hash(focus: str, documents: Sequence[Mapping]) -> str:
    rows = [(d.get("url"), d.get("content_sha256")) for d in list(documents)[:MAX_DOCUMENTS]]
    return hashlib.sha256(json.dumps([focus, PROMPT_VERSION, rows], ensure_ascii=False,
                                     sort_keys=True).encode()).hexdigest()


def _result(status: str, **extra) -> dict:
    return {"engine": VERIFIER_KIND, "status": status, "verified_claim_count": 0,
            "claims": [], "rejected_claims": [], "metered_model_requests": 0,
            "api_requests": 0, "input_tokens": 0, "output_tokens": 0,
            "paid_requests": 0, "paid_spending": False, "skill_activated": False,
            "promotion_performed": False, "authority_granted": False, **extra}


_UNSET = object()


def verify_documents_grounded(root: Path, goal_id: str, focus: str,
                              documents: Sequence[Mapping], *, model=_UNSET,
                              now_epoch: float | None = None) -> dict:
    """Propose, gate and record grounded claims. Never raises for model failures."""
    root = Path(root).resolve()
    policy = load_policy(root)
    if not policy["valid"]:
        return _result("policy_invalid")
    if model is _UNSET:
        model = default_claim_model(root)
    if model is None or not policy["enabled"] or policy["daily_model_requests"] <= 0:
        return _result("disabled")
    goals = LearningGoalStore(root)
    goal = goals.get(goal_id)
    if (goal["status"] != "active" or goal["public_research_allowed"] is not True
            or not isinstance(focus, str) or not 1 <= len(focus) <= 200
            or focus not in {goal["topic"], *(goal.get("study_plan") or [])}):
        return _result("goal_not_authorized")
    passages = select_passages(documents, focus)
    if len({p["host"] for p in passages}) < 2:
        return _result("insufficient_independent_passages")
    when = (datetime.now(timezone.utc) if now_epoch is None
            else datetime.fromtimestamp(now_epoch, timezone.utc))
    usage = _load_usage(root, when.strftime("%Y-%m-%d"))
    digest = _input_hash(focus, documents)
    if digest in usage["seen"]:
        return _result("already_attempted")
    if usage["count"] >= policy["daily_model_requests"]:
        return _result("daily_model_budget_exhausted")
    usage["count"] += 1  # reserve before the request: a failed call still counts
    usage["seen"] = (usage["seen"] + [digest])[-MAX_SEEN:]
    _save_usage(root, usage)
    payload = {"question": focus, "prompt_version": PROMPT_VERSION,
               "passages": [{k: p[k] for k in ("id", "source_id", "host", "url", "text")}
                            for p in passages]}
    result = _result("grounded_claims_rejected", metered_model_requests=1)
    try:
        batch = model.generate("propose", payload, PROPOSAL_SCHEMA)
        proposal = _validate_proposal(batch.data)
        result["input_tokens"] = batch.input_tokens or 0
        result["output_tokens"] = batch.output_tokens or 0
    except ProviderError as exc:
        result.update(status="model_unavailable", reason=str(exc.code))
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, KeyError, AttributeError):
        result.update(status="model_output_invalid")
        return result
    accepted: list[dict] = []
    seen_keys: set[str] = set()
    for claim in proposal["claims"]:
        decision = gate_claim(claim, passages, focus)
        text = " ".join(claim["text"].split())
        key = auto_key(text)
        if decision["accepted"] and key in seen_keys:
            decision = {**decision, "accepted": False, "reasons": ["duplicate_claim"]}
        if decision["accepted"] and len(accepted) >= MAX_ACCEPTED_CLAIMS:
            decision = {**decision, "accepted": False, "reasons": ["claim_limit_reached"]}
        if decision["accepted"]:
            seen_keys.add(key)
            accepted.append({"claim": text, "knowledge_key": key, "decision": decision})
        else:
            result["rejected_claims"].append({"claim": text[:300],
                                              "reasons": decision["reasons"]})
    latest = goals.get(goal_id)
    if (latest["status"] != "active" or latest["public_research_allowed"] is not True
            or latest["topic"] != goal["topic"]):
        result.update(status="research_permission_revoked", rejected_claims=[])
        return result
    store = KnowledgeConsolidationStore(root)
    by_id = {p["id"]: p for p in passages}
    recorded: list[dict] = []
    for item in accepted:
        rows, hosts = [], set()
        for pid in item["decision"]["supporting_ids"]:
            passage = by_id[pid]
            if passage["host"] not in hosts:
                hosts.add(passage["host"])
                rows.append(passage)
        evidence_sha = hashlib.sha256(json.dumps(
            [item["claim"], [(p["url"], p["content_sha256"]) for p in rows]],
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        for passage in rows:
            store.record_evidence(
                item["knowledge_key"], item["claim"],
                evidence_id=stable_evidence_id(evidence_sha, item["claim"], passage["url"]),
                source_id=passage["source_id"], source_url=passage["url"],
                confidence=CONFIDENCE, verifier_kind=VERIFIER_KIND,
                evidence_sha256=evidence_sha, retrieved_at=passage["retrieved_at"],
                verified=True)
        decision = store.consolidate(item["knowledge_key"], stale_after_days=_STALE_AFTER_DAYS)
        if decision.status != "consolidated" or decision.host_count < 2:
            result["rejected_claims"].append({"claim": item["claim"][:300],
                                              "reasons": ["not_consolidated"]})
            continue
        recorded.append({"claim": item["claim"], "knowledge_key": item["knowledge_key"],
                         "source_urls": [p["url"] for p in rows],
                         "evidence_sha256": evidence_sha, "tier": "grounded_local_gate",
                         "confidence": CONFIDENCE, "metrics": item["decision"]["metrics"]})
    evidence = {"schema": EVIDENCE_SCHEMA, "learning_goal_id": goal_id, "question": focus,
                "created_at": utc_now(), "verifier_kind": VERIFIER_KIND,
                "model": {"provider": getattr(model, "name", "unknown"),
                          "id": getattr(model, "model_id", "unknown")},
                "prompt_version": PROMPT_VERSION, "input_sha256": digest,
                "passages": passages, "proposal": proposal, "recorded_claims": recorded,
                "rejected_claims": result["rejected_claims"],
                "metered_model_requests": 1, "paid_requests": 0}
    artifact = _directory(root) / f"{goal_id}_{uuid4().hex}.json"
    write_json(artifact, evidence)
    result.update(artifact=str(artifact), claims=recorded, verified_claim_count=len(recorded))
    if recorded:
        result.update(status="verified_knowledge_recorded", reason="grounded_claim_local_gate")
    return result


def maybe_verify_grounded(root: Path, goal_id: str, focus: str,
                          documents: Sequence[Mapping], *, model=_UNSET,
                          now_epoch: float | None = None) -> dict:
    """Wrapper that can never break a learning attempt."""
    try:
        return verify_documents_grounded(root, goal_id, focus, documents, model=model,
                                         now_epoch=now_epoch)
    except Exception as exc:  # noqa: BLE001 - verification add-on must fail safe
        return _result("grounded_verification_error", reason=type(exc).__name__)
