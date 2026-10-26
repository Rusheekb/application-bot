"""Shared types and HTTP helpers for job sources."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from jobbot import __version__

log = logging.getLogger(__name__)

API_USER_AGENT = f"jobbot/{__version__} (personal job search assistant)"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class SourceError(Exception):
    """A source failed in a way worth showing to the user (bad slug, rate limit, outage...)."""


@dataclass
class JobPosting:
    """A job as returned by a source, normalized to one shape."""

    source: str
    source_id: str
    title: str
    company: str
    url: str
    apply_url: str | None = None
    location: str | None = None
    workplace: str | None = None  # remote / hybrid / onsite
    employment_type: str | None = None
    department: str | None = None
    description: str = ""  # plain text
    posted_at: datetime | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None  # year / month / week / day / hour
    # True when the listing endpoint has no description and fetch_details() must fill it in.
    needs_details: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompanyRef:
    """A company career site hosted on a known applicant tracking system (ATS)."""

    ats: str
    slug: str
    name: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    # Search text for ATSes whose boards are too big to list in full (Workday).
    search_terms: list[str] = field(default_factory=list)


@dataclass
class SearchRef:
    """A keyword search against job boards."""

    keywords: str
    location: str = ""
    remote_only: bool = False
    results: int = 25
    hours_old: int | None = 72
    country: str = "usa"


def is_web_url(url: str | None) -> bool:
    return isinstance(url, str) and url.strip().lower().startswith(("http://", "https://"))


def sanitize_posting(posting: JobPosting) -> bool:
    """Drop postings without a usable http(s) URL and discard non-web apply links."""
    if not is_web_url(posting.url):
        return False
    posting.url = posting.url.strip()
    posting.apply_url = posting.apply_url.strip() if is_web_url(posting.apply_url) else None
    posting.title = " ".join(posting.title.split())
    posting.company = " ".join((posting.company or "").split()) or "Unknown"
    return bool(posting.title)


async def request_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    json: Any = None,
    retries: int = 2,
    not_found_message: str = "not found",
) -> Any:
    """Fetch JSON with retries on network errors, 429 and 5xx; raise SourceError otherwise."""
    merged_headers = {"User-Agent": API_USER_AGENT, "Accept": "application/json"}
    merged_headers.update(headers or {})
    for attempt in range(retries + 1):
        try:
            response = await client.request(method, url, params=params, headers=merged_headers, json=json)
        except httpx.TransportError as exc:
            if attempt < retries:
                await asyncio.sleep(1.5 * 2**attempt)
                continue
            raise SourceError(f"network error: {exc.__class__.__name__}") from exc
        status = response.status_code
        if status == 429 or status >= 500:
            if attempt < retries:
                delay = _retry_after(response) or 2.0 * 2**attempt
                await asyncio.sleep(min(delay, 30.0))
                continue
            raise SourceError("rate limited (HTTP 429)" if status == 429 else f"server error (HTTP {status})")
        if status == 404:
            raise SourceError(not_found_message)
        if status in (401, 403):
            raise SourceError(f"access denied (HTTP {status})")
        if status >= 400:
            raise SourceError(f"HTTP {status}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError("response was not JSON") from exc
    raise SourceError("request failed")  # pragma: no cover - loop always returns or raises


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value and value.isdigit():
        return float(value)
    return None


def new_http_client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
