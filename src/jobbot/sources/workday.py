"""Workday career sites (*.myworkdayjobs.com), via the JSON endpoints the career site itself uses.

Workday boards can hold thousands of postings, so listings are fetched per search term (your
target titles and search keywords) rather than in full.
"""

from __future__ import annotations

import httpx

from jobbot.sources.base import BROWSER_USER_AGENT, CompanyRef, JobPosting, SourceError, request_json
from jobbot.text import html_to_text, parse_datetime, parse_relative_posted, parse_salary

ATS = "workday"
LABEL = "Workday"
PAGE_SIZE = 20
MAX_PER_TERM = 100
MAX_UNFILTERED = 200
_HEADERS = {"User-Agent": BROWSER_USER_AGENT, "Content-Type": "application/json"}


def _base(company: CompanyRef) -> tuple[str, str, str]:
    cfg = company.config
    host, tenant, site = cfg.get("host"), cfg.get("tenant"), cfg.get("site")
    if not (host and tenant and site):
        raise SourceError("Workday company is missing host/tenant/site; re-add it from its careers URL")
    return host, tenant, site


async def fetch_jobs(client: httpx.AsyncClient, company: CompanyRef) -> list[JobPosting]:
    host, tenant, site = _base(company)
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    seen: dict[str, JobPosting] = {}
    terms = company.search_terms or [""]
    limit = MAX_PER_TERM if company.search_terms else MAX_UNFILTERED
    for term in terms:
        offset = 0
        while offset < limit:
            data = await request_json(
                client,
                url,
                method="POST",
                headers=_HEADERS,
                json={"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": term},
                not_found_message=f"Workday site '{site}' not found on {host}",
            )
            postings = data.get("jobPostings") or []
            for item in postings:
                posting = parse_job(item, company)
                if posting and posting.source_id not in seen:
                    seen[posting.source_id] = posting
            offset += PAGE_SIZE
            if len(postings) < PAGE_SIZE or offset >= int(data.get("total") or 0):
                break
    return list(seen.values())


def parse_job(item: dict, company: CompanyRef) -> JobPosting | None:
    host, tenant, site = _base(company)
    path = item.get("externalPath") or ""
    title = (item.get("title") or "").strip()
    if not path or not title:
        return None
    return JobPosting(
        source=ATS,
        source_id=path,
        title=title,
        company=company.name or tenant,
        url=f"https://{host}/{site}{path}",
        apply_url=f"https://{host}/{site}{path}/apply",
        location=item.get("locationsText") or None,
        posted_at=parse_relative_posted(item.get("postedOn")),
        needs_details=True,
        extra={"detail_url": f"https://{host}/wday/cxs/{tenant}/{site}{path}"},
    )


async def fetch_details(client: httpx.AsyncClient, posting: JobPosting) -> None:
    data = await request_json(client, posting.extra["detail_url"], headers=_HEADERS)
    info = data.get("jobPostingInfo") or {}
    posting.description = html_to_text(info.get("jobDescription"))
    posting.location = info.get("location") or posting.location
    posting.employment_type = info.get("timeType") or posting.employment_type
    posting.posted_at = parse_datetime(info.get("startDate")) or posting.posted_at
    posting.url = info.get("externalUrl") or posting.url
    remote_type = (info.get("remoteType") or "").lower()
    if "remote" in remote_type:
        posting.workplace = "remote"
    elif "hybrid" in remote_type:
        posting.workplace = "hybrid"
    org = (data.get("hiringOrganization") or {}).get("name")
    if org and posting.company and posting.company.islower():
        posting.company = org
    if parsed := parse_salary(posting.description):
        posting.salary_min, posting.salary_max, posting.salary_currency, posting.salary_period = parsed
