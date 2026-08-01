"""Shared HTTP plumbing for collectors: a retrying JSON client.

Collectors get an ``httpx.AsyncClient`` with sane timeouts and bounded
exponential-backoff retries on transient failures. Non-transient responses
(4xx, malformed JSON) raise immediately — silent degradation is forbidden
(spec §4.1).

Corporate/dev networks may terminate TLS with a self-signed root (observed on
the author's machine). Set ``DASHBOARD_INSECURE_TLS=1`` to disable verification
*locally only*; it is never set in CI, so GitHub Actions always verifies.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=15.0)
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def make_client(**kwargs: Any) -> httpx.AsyncClient:
    """Construct the shared async client. Honours DASHBOARD_INSECURE_TLS locally."""
    verify = os.environ.get("DASHBOARD_INSECURE_TLS", "") not in ("1", "true", "yes")
    return httpx.AsyncClient(
        timeout=kwargs.pop("timeout", DEFAULT_TIMEOUT),
        verify=verify,
        headers={"User-Agent": "personal-dashboard/0.1 (+https://github.com)"},
        follow_redirects=True,
        **kwargs,
    )


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    max_retries: int = 3,
    backoff_base: float = 0.8,
) -> Any:
    """GET ``url`` and parse JSON, retrying only transient failures.

    Raises ``httpx.HTTPStatusError`` on non-retryable status, ``ValueError`` on
    unparseable JSON, or the last transient error after exhausting retries.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.get(url, params=params)
            if resp.status_code in _RETRYABLE_STATUS:
                resp.raise_for_status()  # -> HTTPStatusError, caught below as transient
            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError as exc:
                raise ValueError(
                    f"non-JSON response from {url} (status {resp.status_code}): "
                    f"{resp.text[:200]!r}"
                ) from exc
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            transient = isinstance(exc, httpx.TransportError) or (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code in _RETRYABLE_STATUS
            )
            if not transient or attempt == max_retries:
                raise
            last_exc = exc
            await asyncio.sleep(backoff_base * (2**attempt))
    # unreachable, but keeps type-checkers happy
    assert last_exc is not None
    raise last_exc
