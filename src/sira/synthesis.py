"""Two-pass evidence synthesis with a deterministic local acceptance gate."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from time import perf_counter
from typing import Protocol
from urllib.parse import urlsplit

from .answer_reports import answer_html, answer_markdown
from .models import ProviderError, utc_now
from .knowledge_consolidation import safe_record_verified_answer_claims
from .retrieval import build_evidence_packet, load_evidence_run
from .storage import Cache, RunStore, code_digest, write_json


PROPOSAL_PROMPT_VERSION = "proposal-v1"
VERIFIER_PROMPT_VERSION = "verifier-v1"
MAX_CLAIMS = 12
MAX_CLAIM_CHARS = 1000

PROPOSAL_SCHEMA = {"type": "object",
 "required": ["answer_language", "claims"], "properties": {
  "answer_language": {"type": "string"},
  "claims": {"type": "array", "maxItems": MAX_CLAIMS, "items": {"type": "object",
   "required": ["id", "text", "support_passage_ids", "opposing_passage_ids"],
   "properties": {"id": {"type": "string"}, "text": {"type": "string"},
    "support_passage_ids": {"type": "array", "items": {"type": "string"}},
    "opposing_passage_ids": {"type": "array", "items": {"type": "string"}}}}}}}

VERDICT_SCHEMA = {"type": "object", "required": ["claims"],
 "properties": {"claims": {"type": "array", "maxItems": MAX_CLAIMS, "items": {"type": "object",
  "required": ["id", "decision", "support_passage_ids",
                                                  "opposing_passage_ids", "reason"],
  "properties": {"id": {"type": "string"},
   "decision": {"type": "string", "enum": ["supported", "mixed", "unsupported"]},
   "support_passage_ids": {"type": "array", "items": {"type": "string"}},
   "opposing_passage_ids": {"type": "array", "items": {"type": "string"}},
   "reason": {"type": "string"}}}}}}


@dataclass(frozen=True)
class ModelBatch:
    data: dict
    api_requests: int = 0
    input_tokens: int | None = 0
    output_tokens: int | None = 0


class SynthesisModel(Protocol):
    name: str
    model_id: str
    cache_namespace: str

    def generate(self, task: str, payload: dict, schema: dict) -> ModelBatch: ...


class InvalidModelOutput(Exception):
    def __init__(self, batch: ModelBatch):
        self.batch = batch


def _ids(value):
    if (not isinstance(value, list) or len(value) > 12
            or any(not isinstance(x, str) or not re.fullmatch(r"E[1-9][0-9]{0,2}", x) for x in value)
            or len(set(value)) != len(value)):
        raise ValueError("invalid passage IDs")
    return value


def _validate_proposal(value):
    if not isinstance(value, dict) or set(value) != {"answer_language", "claims"}:
        raise ValueError("invalid proposal")
    if not isinstance(value["answer_language"], str) or not 1 <= len(value["answer_language"]) <= 32:
        raise ValueError("invalid language")
    claims = value["claims"]
    if not isinstance(claims, list) or len(claims) > MAX_CLAIMS:
        raise ValueError("invalid claims")
    seen = set()
    for item in claims:
        if not isinstance(item, dict) or set(item) != {"id", "text", "support_passage_ids", "opposing_passage_ids"}:
            raise ValueError("invalid claim")
        if (not isinstance(item["id"], str) or not re.fullmatch(r"C(?:[1-9]|1[0-2])", item["id"])
                or item["id"] in seen or not isinstance(item["text"], str)
                or not 1 <= len(item["text"].strip()) <= MAX_CLAIM_CHARS):
            raise ValueError("invalid claim")
        item["text"].encode("utf-8")
        _ids(item["support_passage_ids"])
        _ids(item["opposing_passage_ids"])
        seen.add(item["id"])
    return value


def _validate_verdict(value):
    if not isinstance(value, dict) or set(value) != {"claims"} or not isinstance(value["claims"], list):
        raise ValueError("invalid verdict")
    if len(value["claims"]) > MAX_CLAIMS:
        raise ValueError("too many verdicts")
    seen = set()
    for item in value["claims"]:
        required = {"id", "decision", "support_passage_ids", "opposing_passage_ids", "reason"}
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("invalid verdict item")
        if (not isinstance(item["id"], str) or not re.fullmatch(r"C(?:[1-9]|1[0-2])", item["id"])
                or item["id"] in seen or item["decision"] not in {"supported", "mixed", "unsupported"}
                or not isinstance(item["reason"], str) or len(item["reason"]) > 1000):
            raise ValueError("invalid verdict item")
        item["reason"].encode("utf-8")
        _ids(item["support_passage_ids"])
        _ids(item["opposing_passage_ids"])
        seen.add(item["id"])
    return value


def _cache_key(namespace, stage, evidence_sha, passages, extra):
    data = {"namespace": namespace, "stage": stage, "evidence_sha256": evidence_sha,
            "passages": passages, "extra": extra}
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _model_step(cache, use_cache, key, model, task, payload, schema, validator):
    cached = cache.get(key) if use_cache else None
    if cached is not None:
        try:
            return validator(cached), ModelBatch(cached), 1
        except (ValueError, TypeError, UnicodeError, RecursionError):
            cached = None
    batch = model.generate(task, payload, schema)
    try:
        value = validator(batch.data)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvalidModelOutput(batch) from None
    if use_cache:
        cache.put(key, value)
    return value, batch, 0


def _validate_claim_reasons(proposed, checked, known_passages):
    reasons = []
    if not proposed["support_passage_ids"]: reasons.append("proposal_missing_support")
    if any(x not in known_passages for x in proposed["support_passage_ids"]): reasons.append("proposal_unknown_passage")
    if any(x not in known_passages for x in proposed["opposing_passage_ids"]): reasons.append("proposal_unknown_opposition")
    if proposed["opposing_passage_ids"]: reasons.append("proposal_has_opposition")
    if checked is None: reasons.append("missing_verdict")
    elif checked["decision"] != "supported": reasons.append("verdict_" + checked["decision"])
    if checked and (not checked["support_passage_ids"] or any(x not in known_passages for x in checked["support_passage_ids"])): reasons.append("verifier_invalid_support")
    if checked and set(checked["support_passage_ids"]) != set(proposed["support_passage_ids"]): reasons.append("verifier_support_disagreement")
    if checked and any(x not in known_passages for x in checked["opposing_passage_ids"]): reasons.append("verifier_unknown_opposition")
    if checked and checked["opposing_passage_ids"]:
        reasons.append("verifier_found_opposition")
    return reasons


def answer_run(root: Path, evidence_run_id: str, model: SynthesisModel, *, use_cache=True):
    evidence, evidence_sha, documents = load_evidence_run(root, evidence_run_id)
    passages = build_evidence_packet(evidence["question"], documents)
    passage_rows = [p.as_dict() for p in passages]
    start, run = perf_counter(), RunStore(root)
    run.event("run_started", operation="answer", evidence_run_id=evidence_run_id,
              model=model.name, model_id=model.model_id)
    cache = Cache(root / ".cache" / "synthesis", 86400)
    proposal_data, verdict_data = None, None
    api_requests = cache_hits = input_tokens = output_tokens = 0
    error = None
    try:
        proposal_payload = {"question": evidence["question"], "passages": passage_rows,
                            "prompt_version": PROPOSAL_PROMPT_VERSION}
        proposal_key = _cache_key(model.cache_namespace, "propose", evidence_sha, passage_rows,
                                  PROPOSAL_PROMPT_VERSION)
        proposal_data, batch, hit = _model_step(cache, use_cache, proposal_key, model, "propose",
                                                proposal_payload, PROPOSAL_SCHEMA, _validate_proposal)
        api_requests += batch.api_requests; cache_hits += hit
        input_tokens += batch.input_tokens or 0; output_tokens += batch.output_tokens or 0
        run.event("proposal_finished", cache_hit=bool(hit), api_requests=batch.api_requests)
        verify_payload = {"question": evidence["question"], "passages": passage_rows,
                          "proposal": proposal_data, "prompt_version": VERIFIER_PROMPT_VERSION}
        verdict_key = _cache_key(model.cache_namespace, "verify", evidence_sha, passage_rows,
                                 [VERIFIER_PROMPT_VERSION, proposal_data])
        verdict_data, batch, hit = _model_step(cache, use_cache, verdict_key, model, "verify",
                                               verify_payload, VERDICT_SCHEMA, _validate_verdict)
        api_requests += batch.api_requests; cache_hits += hit
        input_tokens += batch.input_tokens or 0; output_tokens += batch.output_tokens or 0
        run.event("verification_finished", cache_hit=bool(hit), api_requests=batch.api_requests)
    except ProviderError as exc:
        api_requests += int(exc.request_sent)
        error = {"code": exc.code, "retry_after_seconds": exc.retry_after}
        run.event("model_failed", **error)
    except InvalidModelOutput as exc:
        api_requests += exc.batch.api_requests
        input_tokens += exc.batch.input_tokens or 0
        output_tokens += exc.batch.output_tokens or 0
        error = {"code": "invalid_model_output"}
        run.event("model_failed", **error)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        error = {"code": "invalid_model_output"}
        run.event("model_failed", **error)

    accepted, rejected = [], []
    known_passages = {p.id: p for p in passages}
    verdicts = {v["id"]: v for v in verdict_data["claims"]} if verdict_data else {}
    if proposal_data:
        for proposed in proposal_data["claims"]:
            checked = verdicts.get(proposed["id"])
            reasons = _validate_claim_reasons(proposed, checked, known_passages)
            if reasons:
                rejected.append({"id": proposed["id"], "text": proposed["text"], "reasons": reasons,
                                 "proposal": proposed, "verdict": checked})
                continue
            source_ids = list(dict.fromkeys(known_passages[x].source_id for x in proposed["support_passage_ids"]))
            accepted.append({"id": proposed["id"], "text": proposed["text"],
                             "passage_ids": proposed["support_passage_ids"], "source_ids": source_ids})
    cited_sources = list(dict.fromkeys(s for item in accepted for s in item["source_ids"]))
    source_map = {d.source_id: d for d in documents}
    sources = [{"source_id": s, "title": source_map[s].title, "url": source_map[s].url}
               for s in cited_sources]
    status = "failed" if error else "completed" if accepted else "insufficient_evidence"
    hostnames = {urlsplit(s["url"]).hostname for s in sources}
    result = {"schema_version": 1, "kind": "verified_evidence_answer", "run_id": run.run_id,
              "evidence_run_id": evidence_run_id, "evidence_sha256": evidence_sha,
              "code_sha256": code_digest(), "created_at": utc_now(), "question": evidence["question"],
              "status": status, "error": error, "model": {"provider": model.name, "id": model.model_id},
              "prompt_versions": {"proposal": PROPOSAL_PROMPT_VERSION, "verifier": VERIFIER_PROMPT_VERSION},
              "verification": {"independence": "same_model_second_pass", "final_evaluator": False},
              "passages": passage_rows, "proposal": proposal_data, "verdict": verdict_data,
              "accepted_claims": accepted, "rejected_claims": rejected, "sources": sources,
              "metrics": {"retrieval_passages": len(passages), "accepted_claims": len(accepted),
                          "rejected_claims": len(rejected),
                          "structural_citation_coverage": 1.0 if accepted else None,
                          "citation_reference_resolution": 1.0 if accepted else None,
                          "model_verifier_acceptance": len(accepted) / len(proposal_data["claims"])
                                                       if proposal_data and proposal_data["claims"] else None,
                          "source_count": len(sources), "unique_hostnames": len(hostnames),
                          "api_requests": api_requests, "cache_hits": cache_hits,
                          "input_tokens": input_tokens, "output_tokens": output_tokens,
                          "latency_seconds": round(perf_counter() - start, 6),
                          "factual_accuracy": None, "citation_correctness": None,
                          "source_quality": None, "source_diversity": None}}
    result["knowledge_consolidation"] = safe_record_verified_answer_claims(
        root,
        evidence_sha256=evidence_sha,
        accepted_claims=accepted,
        sources=sources,
        verified_at=result["created_at"],
        cache_replay=cache_hits > 0,
    )
    write_json(run.path / "answer.json", result)
    (run.path / "answer.md").write_text(answer_markdown(result), encoding="utf-8")
    (run.path / "answer.html").write_text(answer_html(result), encoding="utf-8")
    run.event("run_finished", status=status, accepted_claims=len(accepted))
    return run.path
