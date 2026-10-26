"""LinkedIn, Indeed, Glassdoor, ZipRecruiter and Google Jobs through the JobSpy scraper
(https://github.com/speedyapply/JobSpy, installed with `pip install "jobbot[scrapers]"`).

These sites do not offer public job-search APIs, so JobSpy scrapes them. Their terms of service
generally prohibit scraping and they rate-limit aggressively: keep result counts and scan
frequency modest. Results that link to a company's own ATS are de-duplicated against the ATS feeds.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from typing import Any

from jobbot.sources.base import JobPosting, SearchRef, SourceError
from jobbot.text import parse_datetime

log = logging.getLogger(__name__)

SITES = {
    "linkedin": "LinkedIn",
    "indeed": "Indeed",
    "glassdoor": "Glassdoor",
    "zip_recruiter": "ZipRecruiter",
    "google": "Google Jobs",
}
_PERIOD = {"yearly": "year", "monthly": "month", "weekly": "week", "daily": "day", "hourly": "hour"}


def available() -> bool:
    try:
        import jobspy  # noqa: F401
    except ImportError:
        return False
    return True


async def search(
    query: SearchRef, sites: list[str], fetch_description: bool = True
) -> tuple[list[JobPosting], dict[str, str]]:
    """Run one JobSpy search across `sites`. Returns (postings, {site: error message})."""
    if not available():
        raise SourceError('JobSpy is not installed; run `pip install "jobbot[scrapers]"`')
    rows, errors = await asyncio.to_thread(_scrape, query, sites, fetch_description)
    postings = [p for row in rows if (p := parse_row(row)) is not None]
    return postings, errors


def _scrape(
    query: SearchRef, sites: list[str], fetch_description: bool
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    from jobspy import scrape_jobs

    where = query.location or ("Remote" if query.remote_only else "")
    google_term = f"{query.keywords} jobs" + (f" near {where}" if where else "")
    if query.hours_old:
        google_term += f" since {max(1, math.ceil(query.hours_old / 24))} days ago"
    try:
        df = scrape_jobs(
            site_name=sites,
            search_term=query.keywords,
            google_search_term=google_term,
            location=query.location or None,
            is_remote=query.remote_only,
            results_wanted=query.results,
            hours_old=query.hours_old,
            country_indeed=query.country,
            fetch_description=fetch_description,
            description_format="markdown",
            verbose=0,
        )
    except ValueError as exc:  # invalid arguments, e.g. an unknown country
        raise SourceError(f"JobSpy rejected the search: {exc}") from exc
    errors = {site: str(err) for site, err in (getattr(df, "attrs", {}) or {}).get("errors", {}).items()}
    return df.to_dict("records"), errors


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    # pandas NaT / numpy NaN behave like NaN without being floats
    try:
        if value != value:  # noqa: PLR0124
            return None
    except (TypeError, ValueError):
        pass
    return value


def _markdown_to_text(md: str) -> str:
    text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!>|])", r"\1", md)  # drop markdown escapes
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^\s*[*\-]\s+", "• ", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\((?:https?://[^)]+)\)", r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_row(raw: dict[str, Any]) -> JobPosting | None:
    row = {k: _clean(v) for k, v in raw.items()}
    site = row.get("site")
    title = (row.get("title") or "").strip()
    url = row.get("job_url")
    if not site or not title or not url:
        return None
    period = _PERIOD.get((row.get("interval") or "").lower()) if row.get("interval") else None
    has_pay = row.get("min_amount") is not None or row.get("max_amount") is not None
    workplace = "remote" if row.get("is_remote") else None
    wfh = (row.get("work_from_home_type") or "").lower()
    if "hybrid" in wfh:
        workplace = "hybrid"
    elif "remote" in wfh:
        workplace = "remote"
    return JobPosting(
        source=site,
        source_id=str(row.get("id") or url),
        title=title,
        company=(row.get("company") or "Unknown").strip(),
        url=url,
        apply_url=row.get("job_url_direct"),
        location=row.get("location"),
        workplace=workplace,
        employment_type=row.get("job_type"),
        description=_markdown_to_text(row.get("description") or ""),
        posted_at=parse_datetime(row.get("date_posted")),
        salary_min=float(row["min_amount"]) if row.get("min_amount") is not None else None,
        salary_max=float(row["max_amount"]) if row.get("max_amount") is not None else None,
        salary_currency=(row.get("currency") or "USD") if has_pay else None,
        salary_period=(period or "year") if has_pay else None,
        extra={"job_level": row.get("job_level")} if row.get("job_level") else {},
    )
