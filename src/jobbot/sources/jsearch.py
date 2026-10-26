"""JSearch (RapidAPI / OpenWeb Ninja): Google for Jobs results, which aggregate LinkedIn, Indeed,
Glassdoor, ZipRecruiter and company career pages through an official API."""

from __future__ import annotations

import math

import httpx

from jobbot.config import Env
from jobbot.sources.adzuna import COUNTRIES
from jobbot.sources.base import JobPosting, SearchRef, SourceError, request_json
from jobbot.text import parse_datetime

KEY = "jsearch"
LABEL = "JSearch"
API = "https://jsearch.p.rapidapi.com/search"
HOST = "jsearch.p.rapidapi.com"
_PERIOD = {"YEAR": "year", "MONTH": "month", "WEEK": "week", "DAY": "day", "HOUR": "hour"}


def configured(env: Env) -> bool:
    return bool(env.jsearch_api_key)


def _date_posted(hours_old: int | None) -> str:
    if not hours_old:
        return "all"
    if hours_old <= 24:
        return "today"
    if hours_old <= 72:
        return "3days"
    if hours_old <= 24 * 7:
        return "week"
    return "month"


async def search(client: httpx.AsyncClient, query: SearchRef, env: Env) -> list[JobPosting]:
    if not configured(env):
        raise SourceError("set JSEARCH_API_KEY (your RapidAPI key) to use JSearch")
    text = f"{query.keywords} in {query.location}" if query.location else query.keywords
    params: dict[str, str | int] = {
        "query": text,
        "page": 1,
        "num_pages": min(5, max(1, math.ceil(query.results / 10))),
        "date_posted": _date_posted(query.hours_old),
        "country": COUNTRIES.get(query.country.lower(), "us"),
    }
    if query.remote_only:
        params["work_from_home"] = "true"
        params["remote_jobs_only"] = "true"
    data = await request_json(
        client,
        API,
        params=params,
        headers={"X-RapidAPI-Key": env.jsearch_api_key or "", "X-RapidAPI-Host": HOST},
    )
    return [p for item in data.get("data") or [] if (p := parse_job(item)) is not None]


def parse_job(item: dict) -> JobPosting | None:
    job_id = str(item.get("job_id") or "")
    title = (item.get("job_title") or "").strip()
    if not job_id or not title:
        return None
    options = item.get("apply_options") or []
    direct = next((o.get("apply_link") for o in options if o.get("is_direct") and o.get("apply_link")), None)
    apply_link = item.get("job_apply_link")
    if item.get("job_apply_is_direct") and apply_link:
        direct = direct or apply_link
    url = apply_link or item.get("job_google_link") or direct
    if not url:
        return None
    location = item.get("job_location") or ", ".join(
        p for p in (item.get("job_city"), item.get("job_state"), item.get("job_country")) if p
    )
    return JobPosting(
        source=KEY,
        source_id=job_id,
        title=title,
        company=(item.get("employer_name") or "Unknown").strip(),
        url=url,
        apply_url=direct,
        location=location or None,
        workplace="remote" if item.get("job_is_remote") else None,
        employment_type=item.get("job_employment_type"),
        description=item.get("job_description") or "",
        posted_at=parse_datetime(
            item.get("job_posted_at_timestamp") or item.get("job_posted_at_datetime_utc")
        ),
        salary_min=item.get("job_min_salary"),
        salary_max=item.get("job_max_salary"),
        salary_currency=item.get("job_salary_currency") or ("USD" if item.get("job_min_salary") else None),
        salary_period=_PERIOD.get((item.get("job_salary_period") or "").upper()),
        extra={"publisher": item.get("job_publisher")},
    )
