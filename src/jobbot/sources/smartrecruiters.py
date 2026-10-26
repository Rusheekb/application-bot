"""SmartRecruiters career sites (public Posting API: https://developers.smartrecruiters.com/docs/posting-api)."""

from __future__ import annotations

import httpx

from jobbot.sources.base import CompanyRef, JobPosting, request_json
from jobbot.text import html_to_text, parse_datetime, parse_salary

ATS = "smartrecruiters"
LABEL = "SmartRecruiters"
API = "https://api.smartrecruiters.com/v1/companies"
PAGE_SIZE = 100
MAX_POSTINGS = 1000


async def fetch_jobs(client: httpx.AsyncClient, company: CompanyRef) -> list[JobPosting]:
    postings: list[JobPosting] = []
    offset = 0
    while offset < MAX_POSTINGS:
        data = await request_json(
            client,
            f"{API}/{company.slug}/postings",
            params={"limit": PAGE_SIZE, "offset": offset},
            not_found_message=f"no SmartRecruiters company named '{company.slug}'",
        )
        content = data.get("content") or []
        postings.extend(p for item in content if (p := parse_job(item, company)) is not None)
        offset += PAGE_SIZE
        if len(content) < PAGE_SIZE or offset >= int(data.get("totalFound") or 0):
            break
    return postings


def parse_job(item: dict, company: CompanyRef) -> JobPosting | None:
    job_id = str(item.get("id") or "")
    title = (item.get("name") or "").strip()
    if not job_id or not title:
        return None
    loc = item.get("location") or {}
    location = loc.get("fullLocation") or ", ".join(
        p for p in (loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()) if p
    )
    workplace = "remote" if loc.get("remote") else ("hybrid" if loc.get("hybrid") else None)
    return JobPosting(
        source=ATS,
        source_id=job_id,
        title=title,
        company=(item.get("company") or {}).get("name") or company.name or company.slug,
        url=f"https://jobs.smartrecruiters.com/{company.slug}/{job_id}",
        location=location or None,
        workplace=workplace,
        employment_type=(item.get("typeOfEmployment") or {}).get("label"),
        department=(item.get("department") or {}).get("label"),
        posted_at=parse_datetime(item.get("releasedDate")),
        needs_details=True,
        extra={"ref": item.get("ref") or f"{API}/{company.slug}/postings/{job_id}"},
    )


async def fetch_details(client: httpx.AsyncClient, posting: JobPosting) -> None:
    data = await request_json(client, posting.extra["ref"])
    sections = ((data.get("jobAd") or {}).get("sections")) or {}
    parts = []
    for key in ("jobDescription", "qualifications", "additionalInformation", "companyDescription"):
        section = sections.get(key) or {}
        body = html_to_text(section.get("text"))
        if body:
            parts.append(f"{section.get('title') or ''}\n{body}".strip())
    posting.description = "\n\n".join(parts)
    posting.url = data.get("postingUrl") or posting.url
    posting.apply_url = data.get("applyUrl") or posting.apply_url
    if (parsed := parse_salary(posting.description)) and posting.salary_min is None:
        posting.salary_min, posting.salary_max, posting.salary_currency, posting.salary_period = parsed
