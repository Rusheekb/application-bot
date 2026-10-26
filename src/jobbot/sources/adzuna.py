"""Adzuna job search API (https://developer.adzuna.com/). Free API key required."""

from __future__ import annotations

import math

import httpx

from jobbot.config import Env
from jobbot.sources.base import JobPosting, SearchRef, SourceError, request_json
from jobbot.text import html_to_text, parse_datetime

KEY = "adzuna"
LABEL = "Adzuna"
API = "https://api.adzuna.com/v1/api/jobs"

# JobSpy-style country names -> Adzuna country codes.
COUNTRIES = {
    "usa": "us", "us": "us", "united states": "us", "uk": "gb", "united kingdom": "gb", "gb": "gb",
    "canada": "ca", "australia": "au", "india": "in", "germany": "de", "france": "fr",
    "netherlands": "nl", "spain": "es", "italy": "it", "poland": "pl", "brazil": "br",
    "mexico": "mx", "singapore": "sg", "new zealand": "nz", "south africa": "za",
    "austria": "at", "belgium": "be", "switzerland": "ch",
}  # fmt: skip
_CURRENCY = {"us": "USD", "gb": "GBP", "ca": "CAD", "au": "AUD", "in": "INR", "sg": "SGD", "nz": "NZD",
             "za": "ZAR", "br": "BRL", "mx": "MXN", "pl": "PLN", "ch": "CHF"}  # fmt: skip


def configured(env: Env) -> bool:
    return bool(env.adzuna_app_id and env.adzuna_app_key)


async def search(client: httpx.AsyncClient, query: SearchRef, env: Env) -> list[JobPosting]:
    if not configured(env):
        raise SourceError("set ADZUNA_APP_ID and ADZUNA_APP_KEY to use Adzuna")
    country = COUNTRIES.get(query.country.lower(), query.country.lower()[:2])
    keywords = f"{query.keywords} remote" if query.remote_only else query.keywords
    params: dict[str, str | int] = {
        "app_id": env.adzuna_app_id or "",
        "app_key": env.adzuna_app_key or "",
        "what": keywords,
        "results_per_page": min(50, max(1, query.results)),
        "sort_by": "date",
    }
    if query.location:
        params["where"] = query.location
    if query.hours_old:
        params["max_days_old"] = max(1, math.ceil(query.hours_old / 24))
    data = await request_json(client, f"{API}/{country}/search/1", params=params)
    return [p for item in data.get("results") or [] if (p := parse_job(item, country)) is not None]


def parse_job(item: dict, country: str = "us") -> JobPosting | None:
    job_id = str(item.get("id") or "")
    title = html_to_text(item.get("title"))
    url = item.get("redirect_url")
    if not job_id or not title or not url:
        return None
    predicted = str(item.get("salary_is_predicted") or "0") == "1"
    return JobPosting(
        source=KEY,
        source_id=job_id,
        title=title,
        company=((item.get("company") or {}).get("display_name") or "Unknown").strip(),
        url=url,
        location=(item.get("location") or {}).get("display_name"),
        employment_type=item.get("contract_time"),
        description=html_to_text(item.get("description")),
        posted_at=parse_datetime(item.get("created")),
        salary_min=None if predicted else item.get("salary_min"),
        salary_max=None if predicted else item.get("salary_max"),
        salary_currency=None if predicted else _CURRENCY.get(country, "EUR"),
        salary_period=None if predicted or not (item.get("salary_min") or item.get("salary_max")) else "year",
        extra={"snippet_only": True},
    )
