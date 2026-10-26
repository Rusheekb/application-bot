"""Remotive remote-jobs API (https://remotive.com/api/remote-jobs). Free; please link back to Remotive."""

from __future__ import annotations

import httpx

from jobbot.config import Env
from jobbot.sources.base import JobPosting, SearchRef, request_json
from jobbot.text import html_to_text, parse_datetime, parse_salary

KEY = "remotive"
LABEL = "Remotive"
API = "https://remotive.com/api/remote-jobs"


def configured(env: Env) -> bool:
    return True


async def search(client: httpx.AsyncClient, query: SearchRef, env: Env) -> list[JobPosting]:
    data = await request_json(client, API, params={"search": query.keywords, "limit": max(1, query.results)})
    return [p for item in data.get("jobs") or [] if (p := parse_job(item)) is not None]


def parse_job(item: dict) -> JobPosting | None:
    job_id = str(item.get("id") or "")
    title = (item.get("title") or "").strip()
    if not job_id or not title or not item.get("url"):
        return None
    salary = parse_salary(item.get("salary") or "")
    region = (item.get("candidate_required_location") or "").strip()
    return JobPosting(
        source=KEY,
        source_id=job_id,
        title=title,
        company=(item.get("company_name") or "Unknown").strip(),
        url=item["url"],
        location=f"Remote ({region})" if region else "Remote",
        workplace="remote",
        employment_type=(item.get("job_type") or "").replace("_", " ") or None,
        department=item.get("category"),
        description=html_to_text(item.get("description")),
        posted_at=parse_datetime(item.get("publication_date")),
        salary_min=salary[0] if salary else None,
        salary_max=salary[1] if salary else None,
        salary_currency=salary[2] if salary else None,
        salary_period=salary[3] if salary else None,
    )
