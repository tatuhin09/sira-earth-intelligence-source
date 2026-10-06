"""Budgeted web-search discovery for learning goals (Tavily, trusted domains only).

Learning goals only ever searched Wikimedia, DOAJ and arXiv. When the owner enables it,
a goal's study question is also sent to the Tavily web search API that SIRA already has a
key for, restricted with ``include_domains`` to hosts SIRA is allowed to read, so no credit
is spent on pages it could not use. Results are only *discovery leads*: they must still be
fetched and independently verified like every other source.

Bounds: disabled by default; a monthly credit ceiling minus an owner reserve (so the
owner's own ``research`` commands keep working); a daily allowance that spreads the
remaining autonomous budget over the rest of the month; results cached for a week (no
repeat spend); a pause after provider errors. Credits are reserved before the request.
Only SIRA's autonomous use is counted locally; leave a reserve for manual commands.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit
from urllib.request import Request
from uuid import uuid4

from .models import ProviderError, parse_sources, utc_now
from .trusted_hosts import load_trusted_hosts

POLICY_SCHEMA = "sira.web_search_policy.v1"
CACHE_TTL_SECONDS = 7 * 86_400
PROVIDER_PAUSE_SECONDS = 3_600
MAX_DOMAINS = 60
MAX_CACHE_FILES = 400
BASE_HOSTS = ("en.wikipedia.org", "arxiv.org", "doaj.org", "www.cisa.gov",
              "cheatsheetseries.owasp.org")


def _directory(root: Path) -> Path:
    return Path(root) / "memory" / "web_search"


# ---------------------------------------------------------------- policy ---
def default_policy() -> dict:
    return {"enabled": False, "monthly_credits": 1000, "owner_reserve": 100,
            "max_results": 3, "valid": True, "source": "default"}


def _policy_ok(value: Mapping) -> bool:
    return (type(value["enabled"]) is bool
            and all(type(value[k]) is int for k in ("monthly_credits", "owner_reserve",
                                                    "max_results"))
            and 1 <= value["monthly_credits"] <= 20_000
            and 0 <= value["owner_reserve"] < value["monthly_credits"]
            and 1 <= value["max_results"] <= 5)


def load_policy(root: Path) -> dict:
    path = _directory(root) / "policy.json"
    if not path.exists() and not path.is_symlink():
        return default_policy()
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            raise ValueError("unsafe policy")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("schema") != POLICY_SCHEMA
                or set(data) != {"schema", "enabled", "monthly_credits", "owner_reserve",
                                 "max_results"} or not _policy_ok(data)):
            raise ValueError("invalid policy")
    except (OSError, ValueError, UnicodeError, TypeError, KeyError):
        return {**default_policy(), "valid": False, "source": "malformed"}
    return {**{k: data[k] for k in ("enabled", "monthly_credits", "owner_reserve",
                                    "max_results")}, "valid": True, "source": "owner_policy"}


def write_policy(root: Path, *, enabled: bool, monthly_credits: int = 1000,
                 owner_reserve: int = 100, max_results: int = 3) -> dict:
    candidate = {"enabled": enabled, "monthly_credits": monthly_credits,
                 "owner_reserve": owner_reserve, "max_results": max_results}
    if not _policy_ok(candidate):
        raise ValueError("monthly 1..20000, reserve 0..monthly-1, max_results 1..5")
    directory = _directory(root)
    if directory.is_symlink() or Path(root, "memory").is_symlink():
        raise ValueError("unsafe policy directory")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".policy.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps({"schema": POLICY_SCHEMA, **candidate}), encoding="utf-8")
    os.replace(temporary, directory / "policy.json")
    return load_policy(root)


# ---------------------------------------------------------------- ledger ---
def _ledger_path(root: Path) -> Path:
    return _directory(root) / "usage.json"


def _load_ledger(root: Path, month: str, day: str) -> dict:
    ledger = {"month": month, "credits_used": 0.0, "requests": 0, "day": day,
              "day_credits": 0.0, "paused_until": 0.0}
    try:
        data = json.loads(_ledger_path(root).read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("month") == month:
            for key in ("credits_used", "day_credits", "paused_until"):
                if isinstance(data.get(key), (int, float)) and data[key] >= 0:
                    ledger[key] = float(data[key])
            if type(data.get("requests")) is int and data["requests"] >= 0:
                ledger["requests"] = data["requests"]
            if data.get("day") != day:
                ledger["day_credits"] = 0.0
        elif isinstance(data, dict) and isinstance(data.get("paused_until"), (int, float)):
            ledger["paused_until"] = float(data["paused_until"])
    except (OSError, ValueError, UnicodeError, TypeError):
        pass
    return ledger


def _save_ledger(root: Path, ledger: Mapping) -> None:
    directory = _directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".usage.{uuid4().hex}.tmp"
    temporary.write_text(json.dumps(dict(ledger)), encoding="utf-8")
    os.replace(temporary, _ledger_path(root))


def budget_status(root: Path, *, now_epoch: float | None = None) -> dict:
    policy = load_policy(root)
    when = datetime.fromtimestamp(time.time() if now_epoch is None else now_epoch,
                                  timezone.utc)
    month, day = when.strftime("%Y-%m"), when.strftime("%Y-%m-%d")
    ledger = _load_ledger(root, month, day)
    autonomous = max(0, policy["monthly_credits"] - policy["owner_reserve"])
    remaining = max(0.0, autonomous - ledger["credits_used"])
    days_left = calendar.monthrange(when.year, when.month)[1] - when.day + 1
    # Fixed for the whole day: the budget left at the start of the day over the days left.
    day_start_remaining = remaining + ledger["day_credits"]
    daily_allowance = math.ceil(day_start_remaining / days_left) if day_start_remaining >= 1 else 0
    return {"month": month, "autonomous_budget": autonomous, "used": ledger["credits_used"],
            "remaining": remaining, "daily_allowance": daily_allowance,
            "used_today": ledger["day_credits"], "paused_until": ledger["paused_until"],
            "policy": {k: policy[k] for k in ("enabled", "monthly_credits", "owner_reserve",
                                              "max_results", "valid", "source")}}


# ---------------------------------------------------------------- search ---
def allowed_domains(root: Path) -> list[str]:
    return sorted({*BASE_HOSTS, *load_trusted_hosts(root)})[:MAX_DOMAINS]


def _tavily_domain_search(root: Path, query: str, max_results: int, domains: Sequence[str]):
    """One bounded request. Returns (sources, credits). No retries, no redirects."""
    from .config import load_key, validate_key
    from .providers.http_json import open_request, reported_credits, request_json
    key = validate_key(load_key(Path(root), "TAVILY_API_KEY"))
    payload = {"query": query, "topic": "general", "search_depth": "basic",
               "auto_parameters": False, "max_results": max_results,
               "include_domains": list(domains), "include_answer": False,
               "include_raw_content": False, "include_images": False, "include_usage": True}
    request = Request("https://api.tavily.com/search", method="POST",
                      data=json.dumps(payload).encode("utf-8"),
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json",
                               "User-Agent": "SIRA/1.2 learning discovery"})
    response = request_json(request, 20, open_request)
    rows = response.get("results")
    if not isinstance(rows, list):
        raise ProviderError("invalid_response", True)
    sources, _rejected = parse_sources(rows[:max_results])
    return sources, reported_credits(response)


_UNREADABLE_DOWNLOAD_SUFFIXES = (".ps", ".zip", ".gz", ".tar", ".tgz", ".doc", ".docx",
                                 ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".epub",
                                 ".mp3", ".mp4")
_ARXIV_ID = re.compile(r"^/(?:abs|html|pdf)/((?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Za-z]{2})?/\d{7}))"
                       r"(?:v\d+)?(?:\.pdf)?/?$")


def normalize_lead_url(url: str) -> str | None:
    """Readable-page form of a search hit, or ``None`` when it is not a readable page.

    arXiv full-text and PDF links become the abstract page (small, HTML, stable). Public
    PDFs remain readable leads; archives, office documents and media are still dropped.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if host == "arxiv.org":
        match = _ARXIV_ID.match(parts.path)
        return f"https://arxiv.org/abs/{match.group(1)}" if match else None
    if parts.path.lower().endswith(_UNREADABLE_DOWNLOAD_SUFFIXES):
        return None
    return url


def _row(source, url: str, retrieved_at: str) -> dict:
    return {"paper_id": "tavily:" + hashlib.sha256(url.encode()).hexdigest()[:16],
            "title": source.title[:300], "abstract": source.excerpt[:1200], "authors": [],
            "year": None, "doi": None, "url": url, "open_access_pdf_url": None,
            "retrieved_at": retrieved_at, "provider": "tavily", "providers": ["tavily"]}


def _cache_file(root: Path, query: str, domains: Sequence[str]) -> Path:
    digest = hashlib.sha256(json.dumps([query.casefold(), list(domains)],
                                       ensure_ascii=False).encode()).hexdigest()
    return _directory(root) / "cache" / f"{digest}.json"


def _result(status: str, **extra: Any) -> dict:
    return {"status": status, "rows": [], "credits_used": 0.0, "api_requests": 0,
            "paid_spending": False, "authority_granted": False, "promotion_performed": False,
            "model_requests": 0, **extra}


def search_trusted_web(root: Path, query: str, *, now_epoch: float | None = None,
                       searcher: Callable[..., Any] | None = None) -> dict:
    """Budget-gated, cache-first Tavily discovery restricted to readable hosts."""
    root = Path(root).resolve()
    policy = load_policy(root)
    if not policy["valid"]:
        return _result("policy_invalid")
    if not policy["enabled"]:
        return _result("disabled")
    if not isinstance(query, str) or not 3 <= len(" ".join(query.split())) <= 300:
        return _result("query_invalid")
    query = " ".join(query.split())
    epoch = time.time() if now_epoch is None else float(now_epoch)
    domains = allowed_domains(root)
    allowed = set(domains)
    cache = _cache_file(root, query, domains)
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if (isinstance(cached, dict) and isinstance(cached.get("rows"), list)
                and isinstance(cached.get("at_epoch"), (int, float))
                and 0 <= epoch - cached["at_epoch"] <= CACHE_TTL_SECONDS):
            rows = [r for r in cached["rows"] if isinstance(r, dict)
                    and urlsplit(str(r.get("url"))).hostname in allowed]
            return _result("cache_hit", rows=rows)
    except (OSError, ValueError, UnicodeError, TypeError):
        pass
    status = budget_status(root, now_epoch=epoch)
    if status["paused_until"] > epoch:
        return _result("provider_paused")
    if status["remaining"] < 1:
        return _result("monthly_budget_exhausted")
    if status["used_today"] >= status["daily_allowance"]:
        return _result("daily_budget_exhausted")
    when = datetime.fromtimestamp(epoch, timezone.utc)
    ledger = _load_ledger(root, when.strftime("%Y-%m"), when.strftime("%Y-%m-%d"))
    ledger["credits_used"] += 1  # reserve before the request; a sent request always counts
    ledger["day_credits"] += 1
    ledger["requests"] += 1
    _save_ledger(root, ledger)
    do_search = searcher or _tavily_domain_search
    try:
        sources, credits = do_search(root, query, policy["max_results"], domains)
    except ProviderError as exc:
        if not exc.request_sent:  # nothing was spent: release the reservation
            ledger["credits_used"] -= 1
            ledger["day_credits"] -= 1
            ledger["requests"] -= 1
        ledger["paused_until"] = epoch + float(exc.retry_after or PROVIDER_PAUSE_SECONDS)
        _save_ledger(root, ledger)
        return _result("provider_error", reason=exc.code,
                       api_requests=1 if exc.request_sent else 0)
    except (ValueError, OSError):
        ledger["credits_used"] -= 1
        ledger["day_credits"] -= 1
        ledger["requests"] -= 1
        _save_ledger(root, ledger)
        return _result("key_unavailable")
    if isinstance(credits, (int, float)) and credits > 1:
        ledger["credits_used"] += float(credits) - 1  # the API reported a higher cost
        _save_ledger(root, ledger)
    retrieved_at = utc_now()
    rows, seen, dropped = [], set(), 0
    for source in sources:
        host = urlsplit(source.url).hostname
        url = normalize_lead_url(source.url) if host in allowed else None
        if url is None:
            dropped += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        rows.append(_row(source, url, retrieved_at))
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        files = sorted(cache.parent.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for stale in files[: max(0, len(files) - MAX_CACHE_FILES + 1)]:
            stale.unlink(missing_ok=True)
        cache.write_text(json.dumps({"at_epoch": epoch, "rows": rows}), encoding="utf-8")
    except OSError:
        pass
    return _result("searched", rows=rows, api_requests=1, leads_dropped=dropped,
                   credits_used=float(credits) if isinstance(credits, (int, float))
                   and credits >= 1 else 1.0)


def maybe_search_trusted_web(root: Path, query: str, *, now_epoch: float | None = None,
                             searcher: Callable[..., Any] | None = None) -> dict:
    """Wrapper that can never break a learning attempt."""
    try:
        return search_trusted_web(root, query, now_epoch=now_epoch, searcher=searcher)
    except Exception as exc:  # noqa: BLE001 - optional discovery must fail safe
        return _result("web_search_error", reason=type(exc).__name__)
