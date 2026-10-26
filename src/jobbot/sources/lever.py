"""Lever job sites (public Postings API: https://github.com/lever/postings-api)."""

from __future__ import annotations

import httpx

from jobbot.sources.base import CompanyRef, JobPosting, request_json
from jobbot.text import html_to_text, parse_datetime, parse_salary

ATS = "lever"
LABEL = "Lever"
API = {"global": "https://api.lever.co/v0/postings", "eu": "https://api.eu.lever.co/v0/postings"}

_WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}
_INTERVAL = {
    "per-year-salary": "year",
    "per-month-salary": "month",
    "per-week-salary": "week",
    "per-day-wage": "day",
    "per-hour-wage": "hour",
}


async def fetch_jobs(client: httpx.AsyncClient, company: CompanyRef) -> list[JobPosting]:
    base = API.get(company.config.get("region", "global"), API["global"])
    data = await request_json(
        client,
        f"{base}/{company.slug}",
        params={"mode": "json"},
        not_found_message=f"no Lever site named '{company.slug}'",
    )
    if not isinstance(data, list):
        return []
    return [p for item in data if (p := parse_job(item, company)) is not None]


def parse_job(item: dict, company: CompanyRef) -> JobPosting | None:
    job_id = str(item.get("id") or "")
    title = (item.get("text") or "").strip()
    if not job_id or not title:
        return None
    categories = item.get("categories") or {}
    location = categories.get("location") or ", ".join(categories.get("allLocations") or []) or None

    parts = [item.get("descriptionPlain") or html_to_text(item.get("description"))]
    for section in item.get("lists") or []:
        body = html_to_text(section.get("content"))
        if body:
            parts.append(f"{section.get('text', '').strip()}\n{body}".strip())
    parts.append(item.get("additionalPlain") or html_to_text(item.get("additional")))
    description = "\n\n".join(p.strip() for p in parts if p and p.strip())

    salary_min = salary_max = None
    currency = period = None
    salary_range = item.get("salaryRange") or {}
    if salary_range.get("min") or salary_range.get("max"):
        salary_min, salary_max = salary_range.get("min"), salary_range.get("max")
        currency = salary_range.get("currency")
        period = _INTERVAL.get(salary_range.get("interval") or "", "year")
    elif parsed := parse_salary(item.get("salaryDescriptionPlain") or description):
        salary_min, salary_max, currency, period = parsed

    hosted = item.get("hostedUrl") or f"https://jobs.lever.co/{company.slug}/{job_id}"
    return JobPosting(
        source=ATS,
        source_id=job_id,
        title=title,
        company=company.name or company.slug,
        url=hosted,
        apply_url=item.get("applyUrl") or f"{hosted}/apply",
        location=location,
        workplace=_WORKPLACE.get((item.get("workplaceType") or "").lower()),
        employment_type=categories.get("commitment"),
        department=categories.get("department") or categories.get("team"),
        description=description,
        posted_at=parse_datetime(item.get("createdAt")),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=currency,
        salary_period=period,
    )
