"""Ashby job boards (public Job Posting API: https://developers.ashbyhq.com/docs/public-job-posting-api)."""

from __future__ import annotations

import httpx

from jobbot.sources.base import CompanyRef, JobPosting, request_json
from jobbot.text import html_to_text, parse_datetime, parse_salary

ATS = "ashby"
LABEL = "Ashby"
API = "https://api.ashbyhq.com/posting-api/job-board"

_WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}
_INTERVAL = {"1 YEAR": "year", "1 MONTH": "month", "1 WEEK": "week", "1 DAY": "day", "1 HOUR": "hour"}


async def fetch_jobs(client: httpx.AsyncClient, company: CompanyRef) -> list[JobPosting]:
    data = await request_json(
        client,
        f"{API}/{company.slug}",
        params={"includeCompensation": "true"},
        not_found_message=f"no Ashby job board named '{company.slug}'",
    )
    return [p for item in data.get("jobs") or [] if (p := parse_job(item, company)) is not None]


def parse_job(item: dict, company: CompanyRef) -> JobPosting | None:
    job_id = str(item.get("id") or "")
    title = (item.get("title") or "").strip()
    if not job_id or not title or item.get("isListed") is False:
        return None

    locations = [item.get("location")] + [s.get("location") for s in item.get("secondaryLocations") or []]
    location = "; ".join(loc for loc in locations if loc) or None
    workplace = _WORKPLACE.get((item.get("workplaceType") or "").lower())
    if workplace is None and item.get("isRemote"):
        workplace = "remote"

    description = item.get("descriptionPlain") or html_to_text(item.get("descriptionHtml"))
    salary_min = salary_max = None
    currency = period = None
    compensation = item.get("compensation") or {}
    for component in compensation.get("summaryComponents") or []:
        if (component.get("compensationType") or "").lower() == "salary":
            salary_min, salary_max = component.get("minValue"), component.get("maxValue")
            currency = component.get("currencyCode")
            period = _INTERVAL.get(component.get("interval") or "", "year")
            break
    if salary_min is None and salary_max is None:
        summary = compensation.get("scrapeableCompensationSalarySummary") or compensation.get(
            "compensationTierSummary"
        )
        if parsed := parse_salary(summary) or parse_salary(description):
            salary_min, salary_max, currency, period = parsed

    url = item.get("jobUrl") or f"https://jobs.ashbyhq.com/{company.slug}/{job_id}"
    return JobPosting(
        source=ATS,
        source_id=job_id,
        title=title,
        company=company.name or company.slug,
        url=url,
        apply_url=item.get("applyUrl") or f"{url}/application",
        location=location,
        workplace=workplace,
        employment_type=item.get("employmentType"),
        department=item.get("department") or item.get("team"),
        description=description,
        posted_at=parse_datetime(item.get("publishedAt")),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=currency,
        salary_period=period,
    )
