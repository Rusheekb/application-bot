"""Workable career pages (the public widget feed behind apply.workable.com)."""

from __future__ import annotations

import httpx

from jobbot.sources.base import CompanyRef, JobPosting, request_json
from jobbot.text import html_to_text, parse_datetime, parse_salary

ATS = "workable"
LABEL = "Workable"
WIDGET_API = "https://apply.workable.com/api/v1/widget/accounts"
DETAIL_API = "https://apply.workable.com/api/v2/accounts"


async def fetch_jobs(client: httpx.AsyncClient, company: CompanyRef) -> list[JobPosting]:
    data = await request_json(
        client,
        f"{WIDGET_API}/{company.slug}",
        params={"details": "true"},
        not_found_message=f"no Workable account named '{company.slug}'",
    )
    name = company.name or data.get("name") or company.slug
    return [p for item in data.get("jobs") or [] if (p := parse_job(item, company, name)) is not None]


def parse_job(item: dict, company: CompanyRef, company_name: str) -> JobPosting | None:
    shortcode = str(item.get("shortcode") or "")
    title = (item.get("title") or "").strip()
    if not shortcode or not title:
        return None
    location = ", ".join(p for p in (item.get("city"), item.get("state"), item.get("country")) if p)
    if not location and item.get("locations"):
        first = item["locations"][0] or {}
        location = ", ".join(p for p in (first.get("city"), first.get("region"), first.get("country")) if p)
    description = html_to_text(item.get("description"))
    salary = parse_salary(description)
    return JobPosting(
        source=ATS,
        source_id=shortcode,
        title=title,
        company=company_name,
        url=item.get("url")
        or item.get("shortlink")
        or f"https://apply.workable.com/{company.slug}/j/{shortcode}/",
        apply_url=item.get("application_url")
        or f"https://apply.workable.com/{company.slug}/j/{shortcode}/apply/",
        location=location or None,
        workplace="remote" if item.get("telecommuting") else None,
        employment_type=item.get("employment_type") or None,
        department=item.get("department") or None,
        description=description,
        posted_at=parse_datetime(item.get("published_on") or item.get("created_at")),
        salary_min=salary[0] if salary else None,
        salary_max=salary[1] if salary else None,
        salary_currency=salary[2] if salary else None,
        salary_period=salary[3] if salary else None,
        needs_details=not description,
        extra={"account": company.slug},
    )


async def fetch_details(client: httpx.AsyncClient, posting: JobPosting) -> None:
    data = await request_json(client, f"{DETAIL_API}/{posting.extra['account']}/jobs/{posting.source_id}")
    parts = [html_to_text(data.get(key)) for key in ("description", "requirements", "benefits")]
    posting.description = "\n\n".join(p for p in parts if p)
    if (parsed := parse_salary(posting.description)) and posting.salary_min is None:
        posting.salary_min, posting.salary_max, posting.salary_currency, posting.salary_period = parsed
