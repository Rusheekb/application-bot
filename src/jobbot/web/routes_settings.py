"""Settings: scanning schedule, AI, auto-apply and notifications."""

from __future__ import annotations

import importlib.util
from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import or_, select

from jobbot.ai.client import MODEL_CHOICES
from jobbot.config import anthropic_credentials_present
from jobbot.db import get_settings, save_settings
from jobbot.models import Job
from jobbot.notify import send_webhook
from jobbot.pipeline import rescore_all
from jobbot.publish import encode_config, export_config
from jobbot.schemas import AISettings, ApplySettings, AppSettings, NotifySettings
from jobbot.sources import adzuna, jobspy_source, jsearch
from jobbot.sources.adzuna import COUNTRIES
from jobbot.sources.base import new_http_client
from jobbot.web.app import get_db, render

router = APIRouter()
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def integrations(env) -> list[dict]:
    playwright = importlib.util.find_spec("playwright") is not None
    return [
        {
            "name": "Claude (Anthropic API)",
            "ok": anthropic_credentials_present(),
            "detail": "AI fit analysis, resume parsing, cover letters and the apply agent",
            "fix": "Add ANTHROPIC_API_KEY to your .env file",
        },
        {
            "name": "JobSpy",
            "ok": jobspy_source.available(),
            "detail": "LinkedIn, Indeed, Glassdoor, ZipRecruiter and Google Jobs searches",
            "fix": 'pip install "jobbot[scrapers]"',
        },
        {
            "name": "Playwright",
            "ok": playwright,
            "detail": "Browser automation for auto-apply",
            "fix": 'pip install "jobbot[apply]" && playwright install chromium',
        },
        {
            "name": "Adzuna",
            "ok": adzuna.configured(env),
            "detail": "Job search API (free key from developer.adzuna.com)",
            "fix": "Add ADZUNA_APP_ID and ADZUNA_APP_KEY to .env",
        },
        {
            "name": "JSearch",
            "ok": jsearch.configured(env),
            "detail": "Google for Jobs results via RapidAPI (covers LinkedIn, Indeed, Glassdoor…)",
            "fix": "Add JSEARCH_API_KEY to .env",
        },
    ]


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = "", error: str = "") -> Response:
    env = request.app.state.env
    with get_db(request).session() as s:
        settings = get_settings(s)
    hosted_config = encode_config(export_config(get_db(request)))
    return render(
        request,
        "settings.html",
        active="settings",
        title="Settings",
        settings=settings,
        integrations=integrations(env),
        models=MODEL_CHOICES,
        efforts=EFFORTS,
        countries=sorted({k for k in COUNTRIES if len(k) > 2} | {"usa", "uk"}),
        hosted_config=hosted_config,
        hosted_size=round(len(hosted_config) / 1024, 1),
        message=msg,
        error=error,
    )


@router.get("/settings/hosted-config")
def hosted_config_file(request: Request) -> Response:
    """Your setup for the hosted job board, to import in its Settings, as a file."""
    return Response(
        encode_config(export_config(get_db(request))),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="jobbot-config.txt"',
            "Cache-Control": "no-store",
        },
    )


def _int(form, name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(str(form.get(name, default)).strip())))
    except ValueError:
        return default


def _float(form, name: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(str(form.get(name, default)).strip())))
    except ValueError:
        return default


@router.post("/settings")
async def save_settings_form(request: Request) -> Response:
    form = await request.form()
    db = get_db(request)
    with db.session() as s:
        current = get_settings(s)
        model = str(form.get("model", current.ai.model)).strip() or current.ai.model
        effort = lambda name, default: v if (v := str(form.get(name, default))) in EFFORTS else default  # noqa: E731
        settings = AppSettings(
            scan_interval_hours=_float(form, "scan_interval_hours", current.scan_interval_hours, 0, 168),
            filters=current.filters,
            jobspy_country=str(form.get("jobspy_country", current.jobspy_country)).strip().lower() or "usa",
            jobspy_hours_old=_int(form, "jobspy_hours_old", current.jobspy_hours_old, 1, 24 * 30),
            ai=AISettings(
                enabled=form.get("ai_enabled") == "on",
                model=model,
                scoring_effort=effort("scoring_effort", current.ai.scoring_effort),
                agent_effort=effort("agent_effort", current.ai.agent_effort),
                max_scores_per_scan=_int(form, "max_scores_per_scan", current.ai.max_scores_per_scan, 0, 500),
                min_heuristic_score=_int(form, "min_heuristic_score", current.ai.min_heuristic_score, 0, 100),
                fallbacks=form.get("fallbacks") == "on",
            ),
            apply=ApplySettings(
                enabled=form.get("apply_enabled") == "on",
                mode="submit" if form.get("apply_mode") == "submit" else "review",
                headless=form.get("headless") == "on",
                auto_after_scan=form.get("auto_after_scan") == "on",
                auto_min_score=_int(form, "auto_min_score", current.apply.auto_min_score, 0, 100),
                daily_limit=_int(form, "daily_limit", current.apply.daily_limit, 0, 100),
                max_steps=_int(form, "max_steps", current.apply.max_steps, 5, 150),
                allow_consent_checkboxes=form.get("allow_consent_checkboxes") == "on",
                live_session_minutes=_int(
                    form, "live_session_minutes", current.apply.live_session_minutes, 5, 720
                ),
            ),
            notify=NotifySettings(
                webhook_url=str(form.get("webhook_url", "")).strip(),
                min_score=_int(form, "notify_min_score", current.notify.min_score, 0, 100),
            ),
        )
        save_settings(s, settings)
    return RedirectResponse("/settings?msg=Settings+saved", status_code=303)


@router.post("/settings/rescore")
def rescore(request: Request) -> Response:
    count = rescore_all(get_db(request))
    return RedirectResponse(
        f"/settings?msg={quote_plus(f'Re-applied filters and scores to {count} jobs')}", status_code=303
    )


@router.post("/settings/test-notification")
async def test_notification(request: Request) -> Response:
    env = request.app.state.env
    with get_db(request).session() as s:
        settings = get_settings(s)
        jobs = list(
            s.scalars(
                select(Job)
                .where(or_(Job.hidden_reason.is_(None), Job.show_anyway.is_(True)))
                .order_by(Job.score.desc().nulls_last())
                .limit(3)
            )
        )
    if not settings.notify.webhook_url:
        return RedirectResponse("/settings?error=Save+a+webhook+URL+first#notifications", status_code=303)
    if not jobs:
        jobs = [
            Job(id=0, title="Example Engineer", company="Example Co", url="https://example.com", score=88)
        ]
    try:
        async with new_http_client(timeout=15) as client:
            await send_webhook(
                client, settings.notify.webhook_url, jobs, env.base_url or f"http://{env.host}:{env.port}"
            )
    except Exception as exc:
        return RedirectResponse(
            f"/settings?error={quote_plus(f'Notification failed: {exc}')}#notifications", status_code=303
        )
    return RedirectResponse("/settings?msg=Test+notification+sent#notifications", status_code=303)
