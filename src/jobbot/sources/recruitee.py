"""Recruitee career sites ({company}.recruitee.com public offers feed)."""

from __future__ import annotations

import httpx

from jobbot.sources.base import CompanyRef, JobPosting, request_json
from jobbot.text import html_to_text, parse_datetime, parse_salary

ATS = "recruitee"
LABEL = "Recruitee"


async def fetch_jobs(client: httpx.AsyncClient, company: CompanyRef) -> list[JobPosting]:
    data = await request_json(
        client,
        f"https://{company.slug}.recruitee.com/api/offers/",
        not_found_message=f"no Recruitee site named '{company.slug}'",
    )
    return [p for item in data.get("offers") or [] if (p := parse_job(item, company)) is not None]


def parse_job(item: dict, company: CompanyRef) -> JobPosting | None:
    job_id = str(item.get("id") or "")
    title = (item.get("title") or "").strip()
    if not job_id or not title:
        return None
    description = "\n\n".join(
        p for p in (html_to_text(item.get("description")), html_to_text(item.get("requirements"))) if p
    )
    salary_min = salary_max = None
    currency = period = None
    salary = item.get("salary") or {}
    try:
        if salary.get("min") or salary.get("max"):
            salary_min = float(salary["min"]) if salary.get("min") else None
            salary_max = float(salary["max"]) if salary.get("max") else None
            currency = salary.get("currency")
            period = {"hour": "hour", "month": "month", "year": "year"}.get(
                salary.get("period") or "", "year"
            )
    except (TypeError, ValueError):
        salary_min = salary_max = None
    if salary_min is None and salary_max is None and (parsed := parse_salary(description)):
        salary_min, salary_max, currency, period = parsed

    workplace = None
    if item.get("remote"):
        workplace = "remote"
    elif item.get("hybrid"):
        workplace = "hybrid"
    url = item.get("careers_url") or f"https://{company.slug}.recruitee.com/o/{item.get('slug', job_id)}"
    return JobPosting(
        source=ATS,
        source_id=job_id,
        title=title,
        company=item.get("company_name") or company.name or company.slug,
        url=url,
        apply_url=item.get("careers_apply_url") or url,
        location=item.get("location") or None,
        workplace=workplace,
        employment_type=item.get("employment_type_code") or None,
        department=item.get("department") or None,
        description=description,
        posted_at=parse_datetime(item.get("published_at") or item.get("created_at")),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=currency,
        salary_period=period,
    )
