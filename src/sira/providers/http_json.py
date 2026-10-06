"""Bounded JSON transport shared by search and extraction."""
from http.client import HTTPException
import json
import math
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, build_opener

from ..models import ProviderError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_request(request, timeout):
    return build_opener(NoRedirect()).open(request, timeout=timeout)


def request_json(request, timeout, opener, max_bytes=2 * 1024 * 1024):
    try:
        with opener(request, timeout=timeout) as response:
            data = response.read(max_bytes + 1)
    except HTTPError as error:
        retry = error.headers.get("Retry-After", "") if error.headers else ""
        error.close()
        raise ProviderError(f"http_{error.code}", True,
                            int(retry) if retry.isdigit() and len(retry) <= 8 else None) from None
    except (TimeoutError, URLError, OSError, HTTPException):
        raise ProviderError("network_or_timeout", True) from None
    if len(data) > max_bytes:
        raise ProviderError("response_too_large", True)
    try:
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        return payload
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProviderError("invalid_response", True) from None


def reported_credits(payload):
    usage = payload.get("usage")
    value = usage.get("credits") if isinstance(usage, dict) else None
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    return value if valid else None
