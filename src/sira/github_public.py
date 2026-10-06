from __future__ import annotations

from dataclasses import dataclass
from email.message import Message
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


_API_ROOT = "https://api.github.com"
_API_VERSION = "2022-11-28"
_DEFAULT_USER_AGENT = "SIRA-GitHub-Public-Research/1"
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class GitHubPublicError(RuntimeError):
    """Base error for the read-only GitHub public research client."""


class GitHubRateLimitError(GitHubPublicError):
    def __init__(
        self,
        message: str,
        *,
        status: int,
        remaining: int | None,
        reset_at: int | None,
        retry_after: int | None,
        resource: str | None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.remaining = remaining
        self.reset_at = reset_at
        self.retry_after = retry_after
        self.resource = resource


class GitHubResponseError(GitHubPublicError):
    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RateLimit:
    limit: int | None
    remaining: int | None
    used: int | None
    reset_at: int | None
    resource: str | None
    retry_after: int | None = None


@dataclass(frozen=True)
class GitHubResult:
    data: Any
    rate_limit: RateLimit
    from_cache: bool
    etag: str | None


Transport = Callable[[Request, float], Any]


def _int_header(headers: Mapping[str, str] | Message, name: str) -> int | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rate_limit(headers: Mapping[str, str] | Message) -> RateLimit:
    return RateLimit(
        limit=_int_header(headers, "x-ratelimit-limit"),
        remaining=_int_header(headers, "x-ratelimit-remaining"),
        used=_int_header(headers, "x-ratelimit-used"),
        reset_at=_int_header(headers, "x-ratelimit-reset"),
        resource=headers.get("x-ratelimit-resource"),
        retry_after=_int_header(headers, "retry-after"),
    )


def _validate_slug(value: str, label: str) -> str:
    if not value or not _SLUG_RE.fullmatch(value):
        raise ValueError(f"invalid GitHub {label}")
    return value


def _validate_repo_path(path: str) -> str:
    if path == "":
        return ""
    if "\\" in path or path.startswith("/"):
        raise ValueError("repository path must be relative")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("repository path contains unsafe segments")
    return "/".join(parts)


def _default_transport(request: Request, timeout: float) -> Any:
    return urlopen(request, timeout=timeout)


class GitHubPublicClient:
    """Read-only GitHub REST client for public research.

    The client exposes only GET operations and never executes downloaded code.
    Authentication is optional and read from an environment variable at request
    time so tokens are not persisted in cache metadata.
    """

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        token_env: str = "SIRA_GITHUB_TOKEN",
        timeout: float = 15.0,
        cache_ttl_seconds: int = 3600,
        max_json_bytes: int = 4 * 1024 * 1024,
        max_text_bytes: int = 1024 * 1024,
        user_agent: str = _DEFAULT_USER_AGENT,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds must be non-negative")
        if max_json_bytes <= 0 or max_text_bytes <= 0:
            raise ValueError("response limits must be positive")
        self.cache_dir = Path(cache_dir)
        self.token_env = token_env
        self.timeout = float(timeout)
        self.cache_ttl_seconds = int(cache_ttl_seconds)
        self.max_json_bytes = int(max_json_bytes)
        self.max_text_bytes = int(max_text_bytes)
        self.user_agent = user_agent
        self._transport = transport or _default_transport
        self._clock = clock

    def search_repositories(
        self,
        query: str,
        *,
        per_page: int = 10,
        page: int = 1,
        sort: str | None = None,
        order: str = "desc",
    ) -> GitHubResult:
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        if not 1 <= per_page <= 50:
            raise ValueError("per_page must be between 1 and 50")
        if page < 1:
            raise ValueError("page must be >= 1")
        params: dict[str, str | int] = {
            "q": query,
            "per_page": per_page,
            "page": page,
        }
        if sort is not None:
            if sort not in {"stars", "forks", "help-wanted-issues", "updated"}:
                raise ValueError("unsupported repository search sort")
            params["sort"] = sort
            if order not in {"asc", "desc"}:
                raise ValueError("order must be asc or desc")
            params["order"] = order
        return self._get_json("/search/repositories", params=params)

    def get_repository(self, owner: str, repo: str) -> GitHubResult:
        owner = _validate_slug(owner, "owner")
        repo = _validate_slug(repo, "repository")
        return self._get_json(f"/repos/{quote(owner)}/{quote(repo)}")

    def list_contents(
        self,
        owner: str,
        repo: str,
        path: str = "",
        *,
        ref: str | None = None,
    ) -> GitHubResult:
        owner = _validate_slug(owner, "owner")
        repo = _validate_slug(repo, "repository")
        safe_path = _validate_repo_path(path)
        endpoint = f"/repos/{quote(owner)}/{quote(repo)}/contents"
        if safe_path:
            endpoint += "/" + "/".join(quote(part) for part in safe_path.split("/"))
        params = {"ref": ref} if ref else None
        return self._get_json(endpoint, params=params)

    def get_text_file(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        ref: str | None = None,
    ) -> GitHubResult:
        owner = _validate_slug(owner, "owner")
        repo = _validate_slug(repo, "repository")
        safe_path = _validate_repo_path(path)
        if not safe_path:
            raise ValueError("file path must not be empty")
        endpoint = (
            f"/repos/{quote(owner)}/{quote(repo)}/contents/"
            + "/".join(quote(part) for part in safe_path.split("/"))
        )
        params = {"ref": ref} if ref else None
        return self._get(
            endpoint,
            params=params,
            accept="application/vnd.github.raw+json",
            decoder=lambda body: body.decode("utf-8"),
            max_bytes=self.max_text_bytes,
        )

    def rate_limit(self) -> GitHubResult:
        return self._get_json("/rate_limit", use_cache=False)

    def _get_json(
        self,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        use_cache: bool = True,
    ) -> GitHubResult:
        return self._get(
            endpoint,
            params=params,
            accept="application/vnd.github+json",
            decoder=lambda body: json.loads(body.decode("utf-8")),
            max_bytes=self.max_json_bytes,
            use_cache=use_cache,
        )

    def _get(
        self,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None,
        accept: str,
        decoder: Callable[[bytes], Any],
        max_bytes: int,
        use_cache: bool = True,
    ) -> GitHubResult:
        if not endpoint.startswith("/") or "://" in endpoint:
            raise ValueError("endpoint must be a GitHub API path")
        query = urlencode(params or {}, doseq=True)
        url = _API_ROOT + endpoint + (f"?{query}" if query else "")
        cache_key = hashlib.sha256(f"{accept}\n{url}".encode("utf-8")).hexdigest()
        body_path = self.cache_dir / f"{cache_key}.body"
        meta_path = self.cache_dir / f"{cache_key}.json"

        cached_meta = self._read_cache_meta(meta_path) if use_cache else None
        cached_body = body_path.read_bytes() if use_cache and body_path.is_file() else None
        now = self._clock()

        if (
            cached_meta is not None
            and cached_body is not None
            and now - float(cached_meta.get("fetched_at", 0)) <= self.cache_ttl_seconds
        ):
            return GitHubResult(
                data=decoder(cached_body),
                rate_limit=RateLimit(**cached_meta.get("rate_limit", {})),
                from_cache=True,
                etag=cached_meta.get("etag"),
            )

        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": _API_VERSION,
            "User-Agent": self.user_agent,
        }
        token = os.environ.get(self.token_env, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if cached_meta and cached_body is not None and cached_meta.get("etag"):
            headers["If-None-Match"] = str(cached_meta["etag"])

        request = Request(url, headers=headers, method="GET")
        try:
            response = self._transport(request, self.timeout)
            try:
                status = int(getattr(response, "status", 200))
                response_headers = response.headers
                body = self._read_bounded(response, max_bytes)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except HTTPError as exc:
            if exc.code == 304 and cached_body is not None and cached_meta is not None:
                rate = _rate_limit(exc.headers)
                cached_meta["fetched_at"] = now
                cached_meta["rate_limit"] = rate.__dict__
                self._write_cache(meta_path, body_path, cached_meta, cached_body)
                return GitHubResult(
                    data=decoder(cached_body),
                    rate_limit=rate,
                    from_cache=True,
                    etag=cached_meta.get("etag"),
                )
            self._raise_http_error(exc)

        rate = _rate_limit(response_headers)
        if status in {403, 429}:
            raise GitHubRateLimitError(
                "GitHub API rate limit reached",
                status=status,
                remaining=rate.remaining,
                reset_at=rate.reset_at,
                retry_after=rate.retry_after,
                resource=rate.resource,
            )
        if status < 200 or status >= 300:
            raise GitHubResponseError(
                f"GitHub API returned HTTP {status}",
                status=status,
            )

        etag = response_headers.get("etag")
        if use_cache:
            meta = {
                "url": url,
                "accept": accept,
                "etag": etag,
                "fetched_at": now,
                "rate_limit": rate.__dict__,
            }
            self._write_cache(meta_path, body_path, meta, body)

        return GitHubResult(
            data=decoder(body),
            rate_limit=rate,
            from_cache=False,
            etag=etag,
        )

    @staticmethod
    def _read_bounded(response: Any, max_bytes: int) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    raise GitHubResponseError(
                        "GitHub response exceeds configured size limit",
                        status=int(getattr(response, "status", 200)),
                    )
            except ValueError:
                pass
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise GitHubResponseError(
                "GitHub response exceeds configured size limit",
                status=int(getattr(response, "status", 200)),
            )
        return body

    @staticmethod
    def _read_cache_meta(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _write_cache(
        self,
        meta_path: Path,
        body_path: Path,
        meta: Mapping[str, Any],
        body: bytes,
    ) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_body = body_path.with_suffix(".body.tmp")
        tmp_meta = meta_path.with_suffix(".json.tmp")
        tmp_body.write_bytes(body)
        tmp_meta.write_text(json.dumps(dict(meta), sort_keys=True), encoding="utf-8")
        tmp_body.replace(body_path)
        tmp_meta.replace(meta_path)

    @staticmethod
    def _raise_http_error(exc: HTTPError) -> None:
        rate = _rate_limit(exc.headers)
        if exc.code in {403, 429} and (
            rate.remaining == 0 or rate.retry_after is not None
        ):
            raise GitHubRateLimitError(
                "GitHub API rate limit reached",
                status=exc.code,
                remaining=rate.remaining,
                reset_at=rate.reset_at,
                retry_after=rate.retry_after,
                resource=rate.resource,
            ) from None
        raise GitHubResponseError(
            f"GitHub API returned HTTP {exc.code}",
            status=exc.code,
        ) from None
