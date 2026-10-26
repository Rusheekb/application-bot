"""Jobs: the review queue, job details, status tracking, AI analysis and scans."""

from __future__ import annotations

import math

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select

from jobbot.ai import LLM, ai_ready, describe_error
from jobbot.ai.context import candidate_block
from jobbot.ai.fit import analyze_fit, apply_fit
from jobbot.ai.writing import draft_cover_letter
from jobbot.apply.support import apply_support
from jobbot.config import anthropic_credentials_present
from jobbot.db import get_profile, get_resume, get_settings
from jobbot.models import Company, Job, ScanRun, SearchQuery, utcnow
from jobbot.schemas import JOB_STATUSES
from jobbot.sources import SOURCE_LABELS
from jobbot.web.app import get_db, get_tasks, render, toast_headers

router = APIRouter()

TABS = [*JOB_STATUSES, "hidden", "all"]
PAGE_SIZE = 40
STATUS_TOASTS = {
    "saved": "Saved",
    "applied": "Marked as applied",
    "dismissed": "Dismissed",
    "new": "Moved back to New",
    "interviewing": "Marked as interviewing",
    "offer": "Offer! Congratulations",
    "rejected": "Marked as rejected",
}


def _visible():
    return or_(Job.hidden_reason.is_(None), Job.show_anyway.is_(True))


def _tab_filter(stmt, tab: str):
    if tab == "hidden":
        return stmt.where(Job.hidden_reason.is_not(None), Job.show_anyway.is_(False))
    if tab == "all":
        return stmt
    stmt = stmt.where(Job.status == tab, _visible())
    if tab == "new":
        stmt = stmt.where(Job.closed_at.is_(None))
    return stmt


def _belongs(job: Job, tab: str) -> bool:
    if tab == "all":
        return True
    if tab == "hidden":
        return job.is_hidden
    return job.status == tab and not job.is_hidden and not (tab == "new" and job.closed_at)


@router.get("/")
def home() -> RedirectResponse:
    return RedirectResponse("/jobs", status_code=303)


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    tab: str = "new",
    q: str = "",
    min_score: int = 0,
    workplace: str = "",
    source: str = "",
    sort: str = "best",
    page: int = 1,
) -> Response:
    tab = tab if tab in TABS else "new"
    db = get_db(request)
    with db.session() as s:
        stmt = _tab_filter(select(Job), tab)
        if q.strip():
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(Job.title.ilike(like), Job.company.ilike(like), Job.location.ilike(like)))
        if min_score:
            stmt = stmt.where(Job.score >= min_score)
        if workplace in ("remote", "hybrid", "onsite"):
            stmt = stmt.where(Job.workplace == workplace)
        if source:
            stmt = stmt.where(Job.source == source)
        total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        if sort == "newest":
            stmt = stmt.order_by(Job.posted_at.desc().nulls_last(), Job.first_seen_at.desc())
        elif sort == "found":
            stmt = stmt.order_by(Job.first_seen_at.desc())
        else:
            stmt = stmt.order_by(Job.score.desc().nulls_last(), Job.posted_at.desc().nulls_last())
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(1, min(page, pages))
        jobs = list(s.scalars(stmt.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)))

        counts = {t: 0 for t in TABS}
        for status, n in s.execute(
            select(Job.status, func.count(Job.id)).where(_visible()).group_by(Job.status)
        ):
            counts[status] = n
        counts["new"] = s.scalar(_tab_filter(select(func.count(Job.id)), "new")) or 0
        counts["hidden"] = s.scalar(_tab_filter(select(func.count(Job.id)), "hidden")) or 0
        counts["all"] = s.scalar(select(func.count(Job.id))) or 0
        sources = [row[0] for row in s.execute(select(Job.source).distinct())]

        profile = get_profile(s)
        settings = get_settings(s)
        setup = {
            "profile_missing": profile.completeness(),
            "no_sources": not (
                s.scalar(select(func.count(Company.id))) or s.scalar(select(func.count(SearchQuery.id)))
            ),
            "no_resume": not get_resume(s),
            "no_ai": settings.ai.enabled and not anthropic_credentials_present(),
            "never_scanned": not s.scalar(select(func.count(ScanRun.id))),
        }
    return render(
        request,
        "jobs.html",
        active="jobs",
        title="Jobs",
        jobs=jobs,
        tab=tab,
        tabs=TABS,
        counts_by_tab=counts,
        q=q,
        min_score=min_score,
        workplace=workplace,
        source=source,
        sort=sort,
        page=page,
        pages=pages,
        total=total,
        sources=sorted(sources),
        source_labels=SOURCE_LABELS,
        setup=setup,
        apply_enabled=settings.apply.enabled,
    )


def _get_job(request: Request, s, job_id: int) -> Job:
    job = s.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int) -> Response:
    db = get_db(request)
    tasks = get_tasks(request)
    with db.session() as s:
        job = _get_job(request, s, job_id)
        runs = list(job.application_runs)
        settings = get_settings(s)
        profile = get_profile(s)
        live_ids = {r.id for r in runs if tasks.apply.is_live(r.id)}
    return render(
        request,
        "job.html",
        active="jobs",
        title=f"{job.title} · {job.company}",
        job=job,
        runs=runs,
        live_ids=live_ids,
        settings=settings,
        profile=profile,
        ai_available=ai_ready(settings.ai),
        support=apply_support(job.ats),
        statuses=JOB_STATUSES,
    )


@router.post("/jobs/{job_id}/status")
def set_status(request: Request, job_id: int, status: str = Form(...), tab: str = Form("")) -> Response:
    if status not in JOB_STATUSES:
        raise HTTPException(400, "Unknown status")
    if tab not in (*TABS, "detail"):
        tab = ""
    db = get_db(request)
    with db.session() as s:
        job = _get_job(request, s, job_id)
        job.status = status
        job.status_changed_at = utcnow()
        if status == "applied" and not job.applied_at:
            job.applied_at = utcnow()
        s.flush()
        settings = get_settings(s)
    headers = toast_headers(STATUS_TOASTS.get(status, "Updated"))
    if tab == "detail":
        response = render(request, "partials/status_control.html", job=job, statuses=JOB_STATUSES)
    elif tab and not _belongs(job, tab):
        response = Response(content="", media_type="text/html")
    else:
        response = render(
            request, "partials/job_card.html", job=job, tab=tab, apply_enabled=settings.apply.enabled
        )
    response.headers.update(headers)
    return response


@router.post("/jobs/{job_id}/notes")
def save_notes(request: Request, job_id: int, notes: str = Form("")) -> Response:
    with get_db(request).session() as s:
        _get_job(request, s, job_id).notes = notes.strip() or None
    return Response(status_code=204, headers=toast_headers("Notes saved"))


@router.post("/jobs/{job_id}/show")
def show_anyway(request: Request, job_id: int) -> Response:
    with get_db(request).session() as s:
        _get_job(request, s, job_id).show_anyway = True
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/jobs/{job_id}/analyze", response_class=HTMLResponse)
async def analyze(request: Request, job_id: int) -> Response:
    db = get_db(request)
    with db.session() as s:
        job = _get_job(request, s, job_id)
        profile = get_profile(s)
        settings = get_settings(s)
        resume_text = get_resume(s).get("text", "")
    error = None
    try:
        async with LLM.open(settings.ai) as llm:
            fit = await analyze_fit(llm, job, candidate_block(profile, resume_text))
    except Exception as exc:
        error = describe_error(exc)
    with db.session() as s:
        job = _get_job(request, s, job_id)
        if error:
            job.fit_error = error
        else:
            apply_fit(job, fit, settings.ai.model)
        s.flush()
    return render(request, "partials/fit_panel.html", job=job, ai_available=True, error=error)


@router.post("/jobs/{job_id}/cover-letter", response_class=HTMLResponse)
async def cover_letter(request: Request, job_id: int) -> Response:
    db = get_db(request)
    with db.session() as s:
        job = _get_job(request, s, job_id)
        profile = get_profile(s)
        settings = get_settings(s)
        resume_text = get_resume(s).get("text", "")
    error = None
    try:
        async with LLM.open(settings.ai) as llm:
            letter = await draft_cover_letter(llm, job, profile, resume_text)
    except Exception as exc:
        error = describe_error(exc)
        letter = job.cover_letter
    with db.session() as s:
        job = _get_job(request, s, job_id)
        if not error:
            job.cover_letter = letter
        s.flush()
    return render(request, "partials/cover_letter.html", job=job, error=error, ai_available=True)


@router.post("/jobs/{job_id}/cover-letter/save")
def save_cover_letter(request: Request, job_id: int, cover_letter: str = Form("")) -> Response:
    with get_db(request).session() as s:
        _get_job(request, s, job_id).cover_letter = cover_letter.strip() or None
    return Response(status_code=204, headers=toast_headers("Cover letter saved"))


@router.post("/jobs/{job_id}/delete")
def delete_job(request: Request, job_id: int) -> Response:
    with get_db(request).session() as s:
        s.delete(_get_job(request, s, job_id))
    return Response(status_code=204, headers={"HX-Redirect": "/jobs"})


# -- scans ------------------------------------------------------------------------------------


def _scan_status_context(request: Request) -> dict:
    db = get_db(request)
    tasks = get_tasks(request)
    with db.session() as s:
        last = s.scalars(select(ScanRun).order_by(ScanRun.started_at.desc()).limit(1)).first()
    running = tasks.scan_running
    just_finished = bool(
        last
        and last.finished_at
        and (utcnow() - last.finished_at).total_seconds() < 90
        and (last.stats or {}).get("new")
    )
    return {"running": running, "last": last, "next_at": tasks.next_scan_at, "just_finished": just_finished}


@router.get("/scan/status", response_class=HTMLResponse)
def scan_status(request: Request) -> Response:
    return render(request, "partials/scan_status.html", **_scan_status_context(request))


@router.post("/scan", response_class=HTMLResponse)
async def start_scan(request: Request) -> Response:
    get_tasks(request).start_scan("manual")
    return render(request, "partials/scan_status.html", **_scan_status_context(request))


@router.get("/scans", response_class=HTMLResponse)
def scans_page(request: Request) -> Response:
    with get_db(request).session() as s:
        scans = list(s.scalars(select(ScanRun).order_by(ScanRun.started_at.desc()).limit(50)))
    return render(request, "scans.html", active="sources", title="Scan history", scans=scans)
