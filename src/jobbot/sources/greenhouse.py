"""Greenhouse job boards (public Job Board API: https://developers.greenhouse.io/job-board.html)."""

from __future__ import annotations

import httpx

from jobbot.sources.base import CompanyRef, JobPosting, request_json
from jobbot.text import html_to_text, parse_datetime, parse_salary

ATS = "greenhouse"
LABEL = "Greenhouse"


def _hosts(company: CompanyRef) -> tuple[str, str]:
    """(API base, hosted job board base) for the company's Greenhouse region."""
    if company.config.get("region") == "eu":
        return "https://boards-api.eu.greenhouse.io/v1/boards", "https://job-boards.eu.greenhouse.io"
    return "https://boards-api.greenhouse.io/v1/boards", "https://job-boards.greenhouse.io"


async def fetch_jobs(client: httpx.AsyncClient, company: CompanyRef) -> list[JobPosting]:
    token = company.slug
    api, _ = _hosts(company)
    data = await request_json(
        client,
        f"{api}/{token}/jobs",
        params={"content": "true"},
        not_found_message=f"no Greenhouse board named '{token}'",
    )
    return [p for item in data.get("jobs") or [] if (p := parse_job(item, company)) is not None]


def parse_job(item: dict, company: CompanyRef) -> JobPosting | None:
    token = company.slug
    _, board = _hosts(company)
    job_id = str(item.get("id") or "")
    title = (item.get("title") or "").strip()
    if not job_id or not title:
        return None
    description = html_to_text(item.get("content"))
    salary = parse_salary(description)
    departments = [d.get("name") for d in item.get("departments") or [] if d.get("name")]
    return JobPosting(
        source=ATS,
        source_id=job_id,
        title=title,
        company=company.name or item.get("company_name") or token,
        url=item.get("absolute_url") or f"{board}/{token}/jobs/{job_id}",
        # The Greenhouse-hosted page always carries the application form, even when the company
        # links to its own careers site from absolute_url.
        apply_url=f"{board}/{token}/jobs/{job_id}",
        location=((item.get("location") or {}).get("name") or "").strip() or None,
        department=", ".join(departments) or None,
        description=description,
        posted_at=parse_datetime(item.get("first_published") or item.get("updated_at")),
        salary_min=salary[0] if salary else None,
        salary_max=salary[1] if salary else None,
        salary_currency=salary[2] if salary else None,
        salary_period=salary[3] if salary else None,
    )


async def fetch_board_name(client: httpx.AsyncClient, company: CompanyRef) -> str | None:
    api, _ = _hosts(company)
    data = await request_json(
        client, f"{api}/{company.slug}", not_found_message=f"no Greenhouse board named '{company.slug}'"
    )
    return (data.get("name") or "").strip() or None
