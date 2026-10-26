"""FastAPI application factory, security middleware and template helpers."""

from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select

from jobbot import __version__
from jobbot.apply.manager import describe_status
from jobbot.config import Env, get_env
from jobbot.db import Database
from jobbot.matching import score_band
from jobbot.models import ApplicationRun, Job, utcnow
from jobbot.schemas import STATUS_LABELS
from jobbot.sources import source_label
from jobbot.tasks import TaskManager
from jobbot.text import format_salary, time_ago

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

LIKELIHOOD_LABELS = {
    "high": "High chance",
    "medium": "Decent chance",
    "low": "Long shot",
    "very_low": "Very unlikely",
}


def _ago(value: datetime | None) -> str:
    return time_ago(value)


def _until(value: datetime | None) -> str:
    if value is None:
        return ""
    seconds = int((value - utcnow()).total_seconds())
    if seconds <= 60:
        return "any moment"
    if seconds < 3600:
        return f"in {seconds // 60}m"
    if seconds < 86400:
        return f"in {seconds // 3600}h {seconds % 3600 // 60}m"
    return f"in {seconds // 86400}d"


def _localtime(value: datetime | None) -> str:
    if value is None:
        return ""
    local = value.replace(tzinfo=UTC).astimezone()
    return f"{local:%b} {local.day}, {local.year} {local.strftime('%I:%M %p').lstrip('0')}"


def _safe_href(url: str | None) -> str:
    """Only http(s) links from job sources are rendered as links (no javascript: URLs)."""
    if isinstance(url, str) and url.strip().lower().startswith(("http://", "https://")):
        return url.strip()
    return "#"


def _salary(job: Job) -> str:
    return format_salary(job.salary_min, job.salary_max, job.salary_currency, job.salary_period)


templates.env.filters.update(
    ago=_ago,
    until=_until,
    localtime=_localtime,
    salary=_salary,
    safe_href=_safe_href,
    source_label=source_label,
    status_label=lambda s: STATUS_LABELS.get(s, s),
    band=score_band,
    run_status=describe_status,
    likelihood=lambda v: LIKELIHOOD_LABELS.get(v or "", ""),
)
templates.env.globals.update(version=__version__, STATUS_LABELS=STATUS_LABELS)


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_tasks(request: Request) -> TaskManager:
    return request.app.state.tasks


def nav_counts(db: Database) -> dict[str, int]:
    with db.session() as s:
        new = s.scalar(
            select(func.count(Job.id)).where(
                Job.status == "new",
                Job.closed_at.is_(None),
                or_(Job.hidden_reason.is_(None), Job.show_anyway.is_(True)),
            )
        )
        attention = s.scalar(
            select(func.count(ApplicationRun.id)).where(
                ApplicationRun.status.in_(("ready_for_review", "needs_input"))
            )
        )
    return {"new": new or 0, "attention": attention or 0}


def render(
    request: Request, template: str, active: str = "", status_code: int = 200, **context: Any
) -> Response:
    context.setdefault("title", "jobbot")
    if not template.startswith("partials/"):
        context["counts"] = nav_counts(get_db(request))
    context["active"] = active
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def toast_headers(message: str) -> dict[str, str]:
    import json

    return {"HX-Trigger": json.dumps({"toast": message})}


class SecurityMiddleware:
    """Host allow-list (DNS rebinding), same-origin check for state-changing requests (CSRF),
    and optional HTTP basic auth when JOBBOT_PASSWORD is set."""

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, app: Any, env: Env):
        self.app = app
        self.env = env
        hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
        if env.host not in ("0.0.0.0", "::", ""):
            hosts.add(env.host.lower())
        hosts.update(h.lower() for h in env.extra_allowed_hosts)
        if env.base_url:
            hosts.add((urlparse(env.base_url).hostname or "").lower())
        self.allowed_hosts = hosts
        self.allow_any_host = "*" in hosts

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        host_header = request.headers.get("host", "")
        host = (urlparse(f"//{host_header}").hostname or "").lower()
        if not self.allow_any_host and host not in self.allowed_hosts:
            response = PlainTextResponse(
                f"Host '{host}' is not allowed. Add it to JOBBOT_ALLOWED_HOSTS to use jobbot from this address.",
                status_code=400,
            )
            await response(scope, receive, send)
            return
        if self.env.password and not self._authorized(request):
            response = PlainTextResponse(
                "Authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="jobbot"'},
            )
            await response(scope, receive, send)
            return
        if request.method not in self.SAFE_METHODS and not self._same_origin(request, host_header):
            response = PlainTextResponse("Cross-site request blocked", status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _authorized(self, request: Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode()
        except Exception:
            return False
        _, _, password = decoded.partition(":")
        return secrets.compare_digest(password.encode(), (self.env.password or "").encode())

    @staticmethod
    def _same_origin(request: Request, host_header: str) -> bool:
        origin = request.headers.get("origin")
        if origin:
            if origin == "null":
                return False
            return urlparse(origin).netloc.lower() == host_header.lower()
        referer = request.headers.get("referer")
        if referer:
            return urlparse(referer).netloc.lower() == host_header.lower()
        # Browsers always send Origin on cross-site POSTs; requests without either header come
        # from scripts or the CLI, which aren't a CSRF vector.
        return True


def create_app(
    env: Env | None = None,
    *,
    db: Database | None = None,
    tasks: TaskManager | None = None,
    scheduler: bool = True,
) -> FastAPI:
    env = env or get_env()
    env.ensure_dirs()
    db = db or Database(env.db_path)
    db.create_all()
    tasks = tasks or TaskManager(db, env)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks.recover_after_restart()
        if scheduler:
            tasks.start_scheduler()
        yield
        await tasks.shutdown()

    app = FastAPI(title="jobbot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.env = env
    app.state.db = db
    app.state.tasks = tasks
    app.add_middleware(SecurityMiddleware, env=env)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    from jobbot.web import routes_apply, routes_jobs, routes_profile, routes_settings, routes_sources

    for module in (routes_jobs, routes_profile, routes_sources, routes_settings, routes_apply):
        app.include_router(module.router)
    return app
