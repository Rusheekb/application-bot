"""The public job board for GitHub Pages.

GitHub Pages only serves files, so a scheduled GitHub Actions workflow builds them:

1. read the shared list of job sources (feed/sources.json): company job boards and job-site
   searches, public and the same for everyone,
2. restore the previous run's job history,
3. run a normal scan,
4. write the site: the board page plus jobs.json, the current jobs with what the page needs to
   filter and score them.

Everything personal happens in each visitor's browser. Their settings, scores, saved jobs and
their optional Anthropic API key stay there, so the published files only hold public job postings.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select

from jobbot.config import Env
from jobbot.db import (
    Database,
    get_profile,
    get_resume,
    get_settings,
    save_profile,
    save_resume,
    save_settings,
)
from jobbot.matching import mentions_no_sponsorship, requires_clearance
from jobbot.models import Company, Job, ScanRun, SearchQuery, utcnow
from jobbot.schemas import AppSettings, Profile
from jobbot.sources import DEFAULT_SEARCH_SOURCES, SEARCH_SOURCE_LABELS, CompanyRef, SourceError, source_label
from jobbot.sources.base import is_web_url, new_http_client
from jobbot.sources.detect import detect_company
from jobbot.text import format_salary, truncate

CONFIG_PREFIX = "jobbot1:"
SOURCES_FILE = "feed/sources.json"
ASSETS = Path(__file__).with_name("board")
# The board page and the files it loads. index.html and app.js name the others with ?v=__VERSION__
# so browsers fetch new copies after each update.
BOARD_FILES = ("index.html", "app.css", "app.js", "match.js", "claude.js")
MAX_JOBS = 2500
MAX_DESCRIPTION = 6000
# Jobs not seen in a scan for this long leave the board, and are forgotten a while after that.
STALE_DAYS = 30
FORGET_DAYS = 90
# Company boards list every opening (sales, legal, facilities...). Only titles matching one of
# these, or a search's keywords, are kept, unless sources.json sets its own "keep_titles".
DEFAULT_KEEP_TITLES = [
    "engineer", "developer", "programmer", "scientist", "researcher", "machine learning", "ai",
    "data", "analyst", "architect", "devops", "sre", "product manager", "designer",
]  # fmt: skip


class PublishError(Exception):
    """A setup problem the user has to fix (shown as-is in the workflow log)."""


# -- config ----------------------------------------------------------------------------------


def export_config(db: Database) -> dict[str, Any]:
    """Your setup as one JSON document, to import into the hosted board's Settings."""
    with db.session() as s:
        profile = get_profile(s)
        settings = get_settings(s)
        resume = get_resume(s)
        companies = [
            {
                "name": c.name,
                "ats": c.ats,
                "slug": c.slug,
                "config": c.config or {},
                "careers_url": c.careers_url,
                "enabled": c.enabled,
            }
            for c in s.scalars(select(Company).order_by(Company.id))
        ]
        searches = [
            {
                "keywords": q.keywords,
                "location": q.location,
                "remote_only": q.remote_only,
                "sources": q.sources,
                "results_per_source": q.results_per_source,
                "enabled": q.enabled,
            }
            for q in s.scalars(select(SearchQuery).order_by(SearchQuery.id))
        ]
    return {
        "version": 1,
        "profile": profile.model_dump(mode="json"),
        "settings": settings.model_dump(mode="json", exclude={"apply"}),
        "companies": companies,
        "searches": searches,
        "resume_text": resume.get("text", ""),
    }


def encode_config(config: dict[str, Any]) -> str:
    """Compact text to paste into the hosted board's Settings (gzip + base64)."""
    raw = json.dumps(config, separators=(",", ":"), sort_keys=True).encode()
    return CONFIG_PREFIX + base64.b64encode(gzip.compress(raw, 9)).decode()


def decode_config(value: str) -> dict[str, Any]:
    value = value.strip()
    try:
        if value.startswith("{"):
            data = json.loads(value)
        else:
            value = value.removeprefix(CONFIG_PREFIX)
            data = json.loads(gzip.decompress(base64.b64decode(value, validate=True)))
    except (ValueError, OSError, EOFError) as exc:
        raise PublishError(
            "That config isn't valid: paste the JSON config, or the text from "
            "Settings → Hosted job board (it starts with 'jobbot1:')."
        ) from exc
    if not isinstance(data, dict):
        raise PublishError("The config must be a JSON object")
    return data


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _problems(exc: ValidationError, prefix: str) -> str:
    return "; ".join(f"{'.'.join([prefix, *map(str, e['loc'])])}: {e['msg']}" for e in exc.errors()[:5])


async def import_config(db: Database, config: dict[str, Any]) -> list[str]:
    """Make the database match `config`. Companies may be given as {"url": ...}; their job board
    is detected here. Returns warnings to show the user."""
    warnings: list[str] = []
    profile_doc, settings_doc = config.get("profile") or {}, config.get("settings") or {}
    if not isinstance(profile_doc, dict) or not isinstance(settings_doc, dict):
        raise PublishError("Your settings aren't valid: 'profile' and 'settings' must be objects")
    if not isinstance(config.get("companies") or [], list) or not isinstance(
        config.get("searches") or [], list
    ):
        raise PublishError("Your settings aren't valid: 'companies' and 'searches' must be lists")
    try:
        profile = Profile.model_validate(profile_doc)
    except ValidationError as exc:
        raise PublishError(f"Your profile isn't valid ({_problems(exc, 'profile')})") from exc
    try:
        # Auto-apply needs a browser and your review, so it stays in the local app.
        settings = AppSettings.model_validate({k: v for k, v in settings_doc.items() if k != "apply"})
    except ValidationError as exc:
        raise PublishError(f"Your settings aren't valid ({_problems(exc, 'settings')})") from exc

    companies: list[dict[str, Any]] = []
    async with new_http_client(timeout=20) as client:
        for item in config.get("companies") or []:
            if isinstance(item, str):
                item = {"url": item}
            if not isinstance(item, dict):
                continue
            if item.get("ats") and item.get("slug"):
                companies.append(item)
                continue
            url = str(item.get("url") or item.get("careers_url") or "").strip()
            if not url:
                continue
            try:
                ref: CompanyRef = await detect_company(client, url)
            except SourceError as exc:
                warnings.append(f"Couldn't add {item.get('name') or url}: {exc}")
                continue
            companies.append(
                {
                    "name": item.get("name") or ref.name or ref.slug.split("/")[0].replace("-", " ").title(),
                    "ats": ref.ats,
                    "slug": ref.slug,
                    "config": ref.config,
                    "careers_url": url,
                    "enabled": item.get("enabled", True),
                }
            )

    with db.session() as s:
        save_profile(s, profile)
        save_settings(s, settings)
        save_resume(s, {"filename": "", "path": "", "text": str(config.get("resume_text") or "")})

        existing = {(c.ats, c.slug): c for c in s.scalars(select(Company))}
        wanted = set()
        for item in companies:
            key = (str(item["ats"]), str(item["slug"]))
            if key in wanted:
                continue
            wanted.add(key)
            company = existing.get(key) or Company(ats=key[0], slug=key[1])
            company.name = str(item.get("name") or key[1])[:200]
            company.config = dict(item.get("config") or {})
            company.careers_url = item.get("careers_url")
            company.enabled = bool(item.get("enabled", True))
            s.add(company)
        for key, company in existing.items():
            if key not in wanted:
                s.delete(company)

        existing_searches = {
            (q.keywords.lower(), q.location.lower()): q for q in s.scalars(select(SearchQuery))
        }
        wanted_searches = set()
        for item in config.get("searches") or []:
            if isinstance(item, str):
                item = {"keywords": item}
            if not isinstance(item, dict) or not str(item.get("keywords") or "").strip():
                continue
            keywords = str(item["keywords"]).strip()[:300]
            location = str(item.get("location") or "").strip()[:200]
            key = (keywords.lower(), location.lower())
            if key in wanted_searches:
                continue
            wanted_searches.add(key)
            search = existing_searches.get(key) or SearchQuery(keywords=keywords, location=location)
            requested = item.get("sources") or []
            if isinstance(requested, str):
                requested = [x.strip() for x in requested.split(",")]
            sources = [x for x in requested if x in SEARCH_SOURCE_LABELS]
            if unknown := [str(x) for x in requested if x not in SEARCH_SOURCE_LABELS]:
                warnings.append(
                    f"Search '{keywords}': unknown source(s) {', '.join(unknown)}; "
                    f"choose from {', '.join(SEARCH_SOURCE_LABELS)}"
                )
            search.sources = sources or list(DEFAULT_SEARCH_SOURCES)
            search.remote_only = bool(item.get("remote_only", False))
            search.results_per_source = _int(item.get("results_per_source"), 25, 1, 100)
            search.enabled = bool(item.get("enabled", True))
            s.add(search)
        for key, search in existing_searches.items():
            if key not in wanted_searches:
                s.delete(search)
    return warnings


def forget_old_jobs(db: Database, days: int = FORGET_DAYS) -> int:
    """Keep the saved state small: drop jobs no scan has seen for `days` days."""
    with db.session() as s:
        result = s.execute(delete(Job).where(Job.last_seen_at < utcnow() - timedelta(days=days)))
        return result.rowcount or 0


# -- the shared job sources ------------------------------------------------------------------


def _strings(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PublishError(f"sources: '{name}' must be a list of strings")
    return [v.strip() for v in value if v.strip()]


def load_sources(path: Path) -> dict[str, Any]:
    """Read and check feed/sources.json, the job sources everyone's board shares."""
    try:
        sources = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise PublishError(f"{path} is missing: it lists the companies and searches to scan") from exc
    except ValueError as exc:
        raise PublishError(f"{path} isn't valid JSON: {exc}") from exc
    if not isinstance(sources, dict):
        raise PublishError(f"{path} must hold a JSON object")
    for name in ("companies", "searches"):
        if not isinstance(sources.get(name) or [], list):
            raise PublishError(f"sources: '{name}' must be a list")
    max_age = sources.get("max_age_days", 45)
    if max_age is not None and (not isinstance(max_age, int) or max_age < 1):
        raise PublishError("sources: 'max_age_days' must be a whole number of days, or null")
    keep = _strings(sources.get("keep_titles", DEFAULT_KEEP_TITLES), "keep_titles")
    if not sources.get("companies") and not sources.get("searches"):
        raise PublishError(f"{path} lists no companies or searches, so there's nothing to scan")
    return {
        "companies": sources.get("companies") or [],
        "searches": sources.get("searches") or [],
        "keep_titles": keep,
        "max_age_days": max_age,
        "jobspy_country": str(sources.get("jobspy_country") or "usa"),
        "jobspy_hours_old": _int(sources.get("jobspy_hours_old"), 72, 1, 720),
    }


def feed_config(sources: dict[str, Any]) -> dict[str, Any]:
    """The scan setup for the shared feed: nobody's profile, and no filters beyond posting age.
    Each visitor's own filters and scores are applied in their browser."""
    return {
        "profile": {},
        "settings": {
            "filters": {
                "title_include": sources["keep_titles"],
                "max_years_gap": None,
                "max_age_days": sources["max_age_days"],
                "hide_no_sponsorship": False,
                "hide_clearance_required": False,
            },
            "ai": {"enabled": False},
            "notify": {"webhook_url": ""},
            "jobspy_country": sources["jobspy_country"],
            "jobspy_hours_old": sources["jobspy_hours_old"],
        },
        "companies": sources["companies"],
        "searches": sources["searches"],
    }


# -- job history between runs ----------------------------------------------------------------


def save_state(db_path: Path, state_path: Path) -> None:
    """Keep a compact copy of the database for the next run (it only holds public job data)."""
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "state.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("VACUUM INTO ?", (str(snapshot),))
        finally:
            conn.close()
        data = gzip.compress(snapshot.read_bytes(), 6)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(data)


def restore_state(state_path: Path, db_path: Path) -> str:
    """Put the saved database back. Returns "restored", "missing" or "unreadable"."""
    if not state_path.is_file():
        return "missing"
    try:
        data = gzip.decompress(state_path.read_bytes())
    except (OSError, EOFError):
        return "unreadable"
    if not data.startswith(b"SQLite format 3\0"):
        return "unreadable"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    db_path.write_bytes(data)
    return "restored"


# -- the published board -------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    return value.isoformat(timespec="seconds") + "Z" if value else None


def feed_job(job: Job) -> dict[str, Any]:
    """One job as the board page needs it: the posting plus what the scan already worked out
    (required years, seniority, sponsorship and clearance mentions) so browsers don't redo it."""
    description = job.description or ""
    return {
        "id": job.fingerprint[:16],
        "title": job.title,
        "company": job.company,
        "location": job.location or "",
        "workplace": job.workplace or "",
        "type": job.employment_type or "",
        "salary": format_salary(job.salary_min, job.salary_max, job.salary_currency, job.salary_period),
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "salary_currency": job.salary_currency,
        "salary_period": job.salary_period,
        "posted": _iso(job.posted_at),
        "found": _iso(job.first_seen_at),
        "years": job.years_required,
        "seniority": job.seniority,
        "no_sponsorship": mentions_no_sponsorship(description),
        "clearance": requires_clearance(description),
        "url": job.url if is_web_url(job.url) else None,
        "apply": job.apply_url if is_web_url(job.apply_url) else None,
        "sources": sorted({source_label(e.get("source")) for e in job.sources or []})
        or [source_label(job.source)],
        "description": truncate(description, MAX_DESCRIPTION),
    }


def feed_payload(
    db: Database,
    scan_id: int | None = None,
    notices: list[str] | None = None,
    repository: str | None = None,
) -> dict[str, Any]:
    """jobs.json: every current job in the shared feed, newest first."""
    with db.session() as s:
        scan = s.get(ScanRun, scan_id) if scan_id else None
        if scan is None:
            scan = s.scalars(select(ScanRun).order_by(ScanRun.started_at.desc()).limit(1)).first()
        jobs = list(
            s.scalars(
                select(Job)
                .where(
                    or_(Job.hidden_reason.is_(None), Job.show_anyway.is_(True)),
                    Job.closed_at.is_(None),
                    Job.last_seen_at >= utcnow() - timedelta(days=STALE_DAYS),
                )
                .order_by(func.coalesce(Job.posted_at, Job.first_seen_at).desc())
                .limit(MAX_JOBS)
            )
        )
        companies = s.scalar(select(func.count()).select_from(Company).where(Company.enabled)) or 0
        searches = [q.label for q in s.scalars(select(SearchQuery).where(SearchQuery.enabled))]
        errors = [{"source": "Sources", "error": n} for n in notices or []]
        errors += (scan.errors or []) if scan else []
        return {
            "version": 1,
            "generated": _iso(utcnow()),
            "repository": repository,
            "scan": {
                "status": scan.status if scan else None,
                "started": _iso(scan.started_at) if scan else None,
                "stats": (scan.stats or {}) if scan else {},
                "errors": errors,
            },
            "sources": {"companies": companies, "searches": searches},
            "jobs": [feed_job(j) for j in jobs],
        }


def pages_url(repository: str | None) -> str:
    """Where GitHub Pages serves a repository's site (unless it has a custom domain)."""
    if not repository or "/" not in repository:
        return ""
    owner, name = repository.split("/", 1)
    host = f"{owner.lower()}.github.io"
    return f"https://{host}/" if name.lower() == host else f"https://{host}/{name}/"


def write_site(out: Path, payload: dict[str, Any]) -> None:
    """Write the board page, its scripts and jobs.json."""
    out.mkdir(parents=True, exist_ok=True)
    (out / ".nojekyll").write_text("")
    assets = {name: (ASSETS / name).read_text() for name in BOARD_FILES}
    version = hashlib.sha256("".join(assets.values()).encode()).hexdigest()[:10]
    for name, text in assets.items():
        (out / name).write_text(text.replace("__VERSION__", version))
    (out / "jobs.json").write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


async def publish(
    env: Env,
    out: Path,
    state_path: Path,
    sources_path: Path,
    board_url: str = "",
) -> int:
    """Scan the shared sources and write the site. Returns a process exit code."""
    repository = os.environ.get("GITHUB_REPOSITORY") or None
    sources = load_sources(sources_path)
    env.ensure_dirs()
    restored = restore_state(state_path, env.db_path)
    print(
        {
            "restored": "Restored the job history from the last run.",
            "missing": "No saved job history yet: starting fresh.",
            "unreadable": "Couldn't read the saved job history: starting fresh.",
        }[restored]
    )
    db = Database(env.db_path)
    try:
        db.create_all()
        notices = await import_config(db, feed_config(sources))
        from jobbot.pipeline import Scanner, rescore_all

        rescore_all(db)  # the sources may have changed since the last run
        forget_old_jobs(db)
        scan_id = await Scanner(db, env, board_url=board_url or pages_url(repository)).run("schedule")
        payload = feed_payload(db, scan_id, notices, repository)
        stats, errors = payload["scan"]["stats"], payload["scan"]["errors"]
        print(
            f"Scan {payload['scan']['status']}: {stats.get('fetched', 0)} jobs fetched, "
            f"{stats.get('new', 0)} new, {len(payload['jobs'])} on the board."
        )
        if errors:
            print(f"{len(errors)} source problem(s), listed at the top of the board:")
            for error in errors:
                print(f"- {error['source']}: {error['error']}")
        write_site(out, payload)
    finally:
        db.engine.dispose()
    save_state(env.db_path, state_path)
    print("Published the job board.")
    return 0
