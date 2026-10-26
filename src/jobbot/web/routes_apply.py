"""Applications: start the apply agent, review its work, approve, revise or cancel."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, select

from jobbot.apply.manager import ApplyError
from jobbot.db import get_settings
from jobbot.models import ApplicationRun, Job, utcnow
from jobbot.web.app import get_db, get_tasks, render, toast_headers

router = APIRouter()


@router.post("/jobs/{job_id}/apply")
async def start_apply(request: Request, job_id: int, mode: str = Form("")) -> Response:
    db = get_db(request)
    with db.session() as s:
        if s.get(Job, job_id) is None:
            raise HTTPException(404, "Job not found")
        settings = get_settings(s)
    if not settings.apply.enabled:
        return Response("Auto-apply is turned off in Settings", status_code=400)
    run_mode = mode if mode in ("review", "submit") else settings.apply.mode
    try:
        run_id = await get_tasks(request).apply.enqueue(job_id, mode=run_mode)
    except ApplyError as exc:
        return Response(str(exc), status_code=400)
    return Response(status_code=204, headers={"HX-Redirect": f"/applications/{run_id}"})


@router.get("/applications", response_class=HTMLResponse)
def applications_page(request: Request) -> Response:
    attention = case(
        (ApplicationRun.status.in_(("ready_for_review", "needs_input", "running", "queued")), 0), else_=1
    )
    with get_db(request).session() as s:
        runs = list(
            s.scalars(select(ApplicationRun).order_by(attention, ApplicationRun.created_at.desc()).limit(200))
        )
        rows = [(run, run.job) for run in runs]
    return render(request, "applications.html", active="applications", title="Applications", rows=rows)


def _run_context(request: Request, run_id: int) -> dict:
    tasks = get_tasks(request)
    with get_db(request).session() as s:
        run = s.get(ApplicationRun, run_id)
        if run is None:
            raise HTTPException(404, "Application run not found")
        job = run.job
    working = run_id in tasks.apply.tasks and not tasks.apply.tasks[run_id].done()
    return {"run": run, "job": job, "live": tasks.apply.is_live(run_id), "working": working}


@router.get("/applications/{run_id}", response_class=HTMLResponse)
def application_page(request: Request, run_id: int) -> Response:
    ctx = _run_context(request, run_id)
    return render(
        request, "application.html", active="applications", title=f"Application · {ctx['job'].title}", **ctx
    )


@router.get("/applications/{run_id}/panel", response_class=HTMLResponse)
def application_panel(request: Request, run_id: int) -> Response:
    return render(request, "partials/application_panel.html", **_run_context(request, run_id))


@router.post("/applications/{run_id}/approve")
async def approve(request: Request, run_id: int) -> Response:
    try:
        new_id = await get_tasks(request).apply.approve(run_id)
    except ApplyError as exc:
        return Response(str(exc), status_code=400)
    return Response(status_code=204, headers={"HX-Redirect": f"/applications/{new_id}"})


@router.post("/applications/{run_id}/revise")
async def revise(request: Request, run_id: int, instruction: str = Form(...)) -> Response:
    if not instruction.strip():
        return Response("Describe what to change", status_code=400)
    try:
        await get_tasks(request).apply.revise(run_id, instruction)
    except ApplyError as exc:
        return Response(str(exc), status_code=400)
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/applications/{run_id}/answers")
async def answers(request: Request, run_id: int) -> Response:
    form = await request.form()
    with get_db(request).session() as s:
        run = s.get(ApplicationRun, run_id)
        if run is None:
            raise HTTPException(404)
        questions = list(run.missing_info or [])
    given = {q: str(form.get(f"answer-{i}", "")) for i, q in enumerate(questions)}
    try:
        await get_tasks(request).apply.provide_answers(run_id, given, remember=form.get("remember") == "on")
    except ApplyError as exc:
        return Response(str(exc), status_code=400)
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/applications/{run_id}/cancel")
async def cancel(request: Request, run_id: int) -> Response:
    await get_tasks(request).apply.cancel(run_id)
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/applications/{run_id}/mark-applied")
def mark_applied(request: Request, run_id: int) -> Response:
    """For runs you finished by hand (e.g. after a CAPTCHA)."""
    with get_db(request).session() as s:
        run = s.get(ApplicationRun, run_id)
        if run is None:
            raise HTTPException(404)
        now = utcnow()
        run.job.status = "applied"
        run.job.applied_at = run.job.applied_at or now
        run.job.status_changed_at = now
    return Response(status_code=204, headers={**toast_headers("Marked as applied"), "HX-Refresh": "true"})


@router.get("/applications/{run_id}/screenshot")
def screenshot(request: Request, run_id: int) -> Response:
    env = request.app.state.env
    with get_db(request).session() as s:
        run = s.get(ApplicationRun, run_id)
        name = run.screenshot if run else None
    if not name:
        raise HTTPException(404)
    path = (env.screenshot_dir / Path(name).name).resolve()
    if not path.is_file() or env.screenshot_dir.resolve() not in path.parents:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")


@router.get("/applications/{run_id}/open")
def open_application(request: Request, run_id: int) -> Response:
    """Send you to the application page so you can finish it yourself."""
    with get_db(request).session() as s:
        run = s.get(ApplicationRun, run_id)
        if run is None:
            raise HTTPException(404)
        url = run.final_url or run.start_url or run.job.url
    return RedirectResponse(url, status_code=303)
