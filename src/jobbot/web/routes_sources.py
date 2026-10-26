"""Sources: job-board searches, company career sites and hard filters."""

from __future__ import annotations

import re
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from jobbot.config import Env
from jobbot.db import get_profile, get_settings, save_settings
from jobbot.matching import is_relevant, relevance_terms
from jobbot.models import Company, SearchQuery, utcnow
from jobbot.pipeline import rescore_all
from jobbot.schemas import Filters
from jobbot.sources import (
    API_SEARCH_SOURCES,
    COMPANY_SOURCES,
    DEFAULT_SEARCH_SOURCES,
    JOBSPY_SITES,
    SEARCH_SOURCE_LABELS,
    CompanyRef,
    SourceError,
    jobspy_source,
    source_label,
)
from jobbot.sources import greenhouse as greenhouse_source
from jobbot.sources.base import new_http_client
from jobbot.sources.detect import detect_company
from jobbot.web.app import get_db, render, toast_headers

router = APIRouter()


def source_availability(env: Env) -> dict[str, str]:
    """Why a search source can't run right now ('' when it can)."""
    scraper = "" if jobspy_source.available() else 'needs JobSpy: pip install "jobbot[scrapers]"'
    notes = dict.fromkeys(JOBSPY_SITES, scraper)
    for key, module in API_SEARCH_SOURCES.items():
        notes[key] = ""
        if not module.configured(env):
            notes[key] = {
                "adzuna": "needs ADZUNA_APP_ID and ADZUNA_APP_KEY in .env",
                "jsearch": "needs JSEARCH_API_KEY (your RapidAPI key) in .env",
            }.get(key, "not configured")
    return notes


@router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, msg: str = "", error: str = "") -> Response:
    env = request.app.state.env
    with get_db(request).session() as s:
        searches = list(s.scalars(select(SearchQuery).order_by(SearchQuery.created_at)))
        companies = list(s.scalars(select(Company).order_by(Company.name)))
        settings = get_settings(s)
        profile = get_profile(s)
    return render(
        request,
        "sources.html",
        active="sources",
        title="Sources",
        searches=searches,
        companies=companies,
        filters=settings.filters,
        profile=profile,
        search_sources=SEARCH_SOURCE_LABELS,
        default_sources=DEFAULT_SEARCH_SOURCES,
        availability=source_availability(env),
        ats_labels={k: m.LABEL for k, m in COMPANY_SOURCES.items()},
        message=msg,
        error=error,
    )


@router.post("/searches")
async def add_search(request: Request) -> Response:
    form = await request.form()
    keywords = str(form.get("keywords", "")).strip()
    if not keywords:
        return RedirectResponse("/sources?error=Enter+keywords+for+the+search#searches", status_code=303)
    sources = [s for s in form.getlist("sources") if s in SEARCH_SOURCE_LABELS] or list(
        DEFAULT_SEARCH_SOURCES
    )
    try:
        results = max(5, min(100, int(str(form.get("results_per_source", "25")))))
    except ValueError:
        results = 25
    with get_db(request).session() as s:
        s.add(
            SearchQuery(
                keywords=keywords[:300],
                location=str(form.get("location", "")).strip()[:200],
                remote_only=form.get("remote_only") == "on",
                sources=sources,
                results_per_source=results,
            )
        )
    return RedirectResponse("/sources?msg=Search+added#searches", status_code=303)


@router.post("/searches/{search_id}/toggle")
def toggle_search(request: Request, search_id: int) -> Response:
    with get_db(request).session() as s:
        search = s.get(SearchQuery, search_id)
        if search is None:
            raise HTTPException(404)
        search.enabled = not search.enabled
    return RedirectResponse("/sources#searches", status_code=303)


@router.post("/searches/{search_id}/delete")
def delete_search(request: Request, search_id: int) -> Response:
    with get_db(request).session() as s:
        search = s.get(SearchQuery, search_id)
        if search is not None:
            s.delete(search)
    return RedirectResponse("/sources#searches", status_code=303)


def _pretty_name(ref: CompanyRef) -> str:
    base = ref.name or ref.slug.split("/")[0]
    return re.sub(r"[-_]+", " ", base).strip().title()


async def _create_company(request: Request, url: str, name: str = "") -> tuple[Company | None, str]:
    """Detect the ATS behind `url` and add the company. Returns (company, error)."""
    url = url.strip()
    if not url:
        return None, "enter a careers page URL"
    async with new_http_client(timeout=20) as client:
        try:
            ref = await detect_company(client, url)
        except SourceError as exc:
            return None, str(exc)
        if not name and ref.ats == "greenhouse":
            try:
                name = await greenhouse_source.fetch_board_name(client, ref) or ""
            except SourceError:
                name = ""
    with get_db(request).session() as s:
        existing = s.scalars(select(Company).where(Company.ats == ref.ats, Company.slug == ref.slug)).first()
        if existing is not None:
            return existing, f"{existing.name} is already on your list"
        company = Company(
            name=(name.strip() or _pretty_name(ref))[:200],
            ats=ref.ats,
            slug=ref.slug,
            config=ref.config,
            careers_url=url if "://" in url else f"https://{url}",
        )
        s.add(company)
        s.flush()
        return company, ""


@router.post("/companies")
async def add_company(request: Request, url: str = Form(""), name: str = Form("")) -> Response:
    company, error = await _create_company(request, url, name)
    if company is None:
        return RedirectResponse(f"/sources?error={quote_plus(error)}#companies", status_code=303)
    if error:
        return RedirectResponse(f"/sources?msg={quote_plus(error)}#companies", status_code=303)
    message = f"Added {company.name} ({source_label(company.ats)}). Use Check to test it."
    return RedirectResponse(f"/sources?msg={quote_plus(message)}#companies", status_code=303)


@router.post("/companies/bulk")
async def add_companies_bulk(request: Request, urls: str = Form("")) -> Response:
    added, problems = [], []
    for line in [u.strip() for u in urls.splitlines() if u.strip()][:100]:
        company, error = await _create_company(request, line)
        if company is not None and not error:
            added.append(company.name)
        elif error:
            problems.append(f"{line}: {error}")
    message = f"Added {len(added)} compan{'y' if len(added) == 1 else 'ies'}."
    if problems:
        message += " Skipped: " + "; ".join(problems[:5]) + (" …" if len(problems) > 5 else "")
    return RedirectResponse(f"/sources?msg={quote_plus(message)}#companies", status_code=303)


@router.post("/companies/{company_id}/toggle")
def toggle_company(request: Request, company_id: int) -> Response:
    with get_db(request).session() as s:
        company = s.get(Company, company_id)
        if company is None:
            raise HTTPException(404)
        company.enabled = not company.enabled
    return RedirectResponse("/sources#companies", status_code=303)


@router.post("/companies/{company_id}/delete")
def delete_company(request: Request, company_id: int) -> Response:
    with get_db(request).session() as s:
        company = s.get(Company, company_id)
        if company is not None:
            s.delete(company)
    return RedirectResponse("/sources#companies", status_code=303)


@router.post("/companies/{company_id}/check", response_class=HTMLResponse)
async def check_company(request: Request, company_id: int) -> Response:
    """Fetch the company's board now and report how many jobs match your titles."""
    db = get_db(request)
    with db.session() as s:
        company = s.get(Company, company_id)
        if company is None:
            raise HTTPException(404)
        profile = get_profile(s)
        settings = get_settings(s)
        searches = list(s.scalars(select(SearchQuery).where(SearchQuery.enabled)))
    terms = relevance_terms(settings.filters, profile, [q.keywords for q in searches])
    module = COMPANY_SOURCES.get(company.ats)
    ref = CompanyRef(
        company.ats,
        company.slug,
        company.name,
        dict(company.config or {}),
        search_terms=terms if company.ats == "workday" else [],
    )
    try:
        async with new_http_client() as client:
            postings = await module.fetch_jobs(client, ref)
        matching = [p for p in postings if is_relevant(p.title, terms)]
        status = f"{len(postings)} open job{'s' if len(postings) != 1 else ''}, {len(matching)} matching your titles"
        sample = ", ".join(p.title for p in matching[:3])
        if sample:
            status += f" (e.g. {sample})"
        ok = True
    except Exception as exc:
        status = (
            f"Error: {exc}" if isinstance(exc, SourceError) else f"Error: {exc.__class__.__name__}: {exc}"
        )
        postings, ok = [], False
    with db.session() as s:
        company = s.get(Company, company_id)
        company.last_checked_at = utcnow()
        company.last_status = status
        if ok:
            company.last_job_count = len(postings)
        s.flush()
    response = render(
        request,
        "partials/company_row.html",
        company=company,
        ats_labels={k: m.LABEL for k, m in COMPANY_SOURCES.items()},
    )
    response.headers.update(toast_headers(status if ok else "Check failed"))
    return response


@router.post("/filters")
async def save_filters(request: Request) -> Response:
    form = await request.form()

    def items(name: str) -> list[str]:
        return [x.strip() for x in re.split(r"[\n,]", str(form.get(name, ""))) if x.strip()]

    def number(name: str) -> int | None:
        value = re.sub(r"[^\d]", "", str(form.get(name, "")))
        return int(value) if value else None

    filters = Filters(
        title_include=items("title_include"),
        title_exclude=items("title_exclude"),
        description_exclude=items("description_exclude"),
        company_exclude=items("company_exclude"),
        locations=items("locations"),
        location_exclude=items("location_exclude"),
        include_remote=form.get("include_remote") == "on",
        min_salary=number("min_salary"),
        max_years_gap=number("max_years_gap") if str(form.get("max_years_gap", "")).strip() != "" else None,
        max_age_days=number("max_age_days"),
        hide_no_sponsorship=form.get("hide_no_sponsorship") == "on",
        hide_clearance_required=form.get("hide_clearance_required") == "on",
    )
    db = get_db(request)
    with db.session() as s:
        settings = get_settings(s)
        settings.filters = filters
        save_settings(s, settings)
    count = rescore_all(db)
    return RedirectResponse(
        f"/sources?msg={quote_plus(f'Filters saved and re-applied to {count} jobs')}#filters", status_code=303
    )
