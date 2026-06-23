"""HTTP publish client (Phase 0 task B13), lifted from the monolith's _publish_to_certumalink.

Credentials are INJECTED (base_url/token args), never read from os.environ. The opener is
injectable so tests run without network. A non-2xx returns a result dict (the caller decides how
to react); a transport failure raises PublishError.
"""
from __future__ import annotations

import json
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from certuma_core.urls import claim_urls_by_npi  # re-used for response parsing

__all__ = ["PublishError", "publish", "publish_summary", "claim_urls_by_npi"]

_ENDPOINT_PATH = "/api/admin/imports/physician-profiles"
_USER_AGENT = "certuma-reach-publish/0.1"


class PublishError(RuntimeError):
    """Transport-level failure reaching the publish endpoint (timeout / DNS / connection)."""


def publish(
    payload: dict,
    *,
    base_url: str,
    token: str,
    timeout: int = 30,
    opener: Optional[Callable] = None,
) -> dict:
    """POST the payload. Returns {ok, status, endpoint, response}. Raises PublishError on transport failure."""
    if not base_url:
        raise ValueError("base_url is required to publish")
    if not token:
        raise ValueError("token is required to publish")
    endpoint = f"{base_url.rstrip('/')}{_ENDPOINT_PATH}"
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )
    _open = opener or urlopen
    try:
        with _open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
            parsed = json.loads(text) if text else {}
            if not isinstance(parsed, dict):
                parsed = {"response": parsed}
            status = int(response.status)
            return {"ok": 200 <= status < 300, "status": status, "endpoint": endpoint, "response": parsed}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed_error = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed_error = {"message": body}
        return {"ok": False, "status": int(exc.code), "endpoint": endpoint, "response": parsed_error}
    except (URLError, TimeoutError) as exc:
        raise PublishError(f"Certumalink publish request failed: {exc}") from exc


def publish_summary(result: Optional[dict]) -> dict:
    """Reconciliation record from a publish result (lifted from the monolith's _publish_summary)."""
    if result is None:
        return {"attempted": False}
    response = result.get("response")
    response_map = response if isinstance(response, dict) else {}
    return {
        "attempted": True,
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "import_id": response_map.get("import_id") or response_map.get("id") or "",
        "created": response_map.get("created_count", 0),
        "updated": response_map.get("updated_count", 0),
        "unchanged": response_map.get("unchanged_count", 0),
        "skipped": response_map.get("skipped_count", 0),
        "errors": response_map.get("error_count", 0),
        "claim_links": len(claim_urls_by_npi(result)),
    }
