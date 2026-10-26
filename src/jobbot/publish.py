"""A hosted, read-only job board for GitHub Pages.

GitHub Pages only serves static files, so a scheduled GitHub Actions workflow does the work:

1. read your setup (profile, filters, companies, searches) from the JOBBOT_CONFIG secret,
2. restore the previous run's database from an encrypted state file,
3. run a normal scan,
4. publish the matches as a single static page, encrypted with your passphrase
   (JOBBOT_PAGE_PASSWORD) because Pages sites are public,
5. save the database again, encrypted, for the next run.

Nothing personal is ever written anywhere unencrypted, and the workflow log (public for public
repositories) only gets counts. Without an Anthropic API key the board uses the built-in
estimated score, so the whole setup runs for free.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import html
import json
import os
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, or_, select

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
from jobbot.models import Company, Job, ScanRun, SearchQuery, utcnow
from jobbot.schemas import AppSettings, Profile
from jobbot.sources import DEFAULT_SEARCH_SOURCES, SEARCH_SOURCE_LABELS, CompanyRef, SourceError, source_label
from jobbot.sources.base import is_web_url, new_http_client
from jobbot.sources.detect import detect_company
from jobbot.text import format_salary, truncate

CONFIG_PREFIX = "jobbot1:"
PBKDF2_ITERATIONS = 600_000
MIN_PASSPHRASE = 12
ASSETS = Path(__file__).with_name("board")
MAX_JOBS = 400
MAX_HIDDEN = 150
# Jobs not seen in a scan for this long leave the board, and are forgotten a while after that.
STALE_DAYS = 30
FORGET_DAYS = 90
GITHUB_SECRET_LIMIT = 48 * 1024


class PublishError(Exception):
    """A setup problem the user has to fix (shown as-is in the workflow log)."""


# -- config ----------------------------------------------------------------------------------


def export_config(db: Database) -> dict[str, Any]:
    """Everything a hosted scan needs, as one JSON document."""
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
    """Compact form for a GitHub secret (gzip + base64)."""
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
            "JOBBOT_CONFIG isn't valid: paste the JSON config, or the text from "
            "Settings → Hosted job board (it starts with 'jobbot1:')."
        ) from exc
    if not isinstance(data, dict):
        raise PublishError("JOBBOT_CONFIG must be a JSON object")
    return data


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


async def import_config(db: Database, config: dict[str, Any]) -> list[str]:
    """Make the database match `config`. Companies may be given as {"url": ...}; their job board
    is detected here. Returns warnings to show the user."""
    warnings: list[str] = []
    settings_doc = config.get("settings") or {}
    if not isinstance(settings_doc, dict):
        raise PublishError("JOBBOT_CONFIG: 'settings' must be an object")
    try:
        profile = Profile.model_validate(config.get("profile") or {})
        # Auto-apply needs a browser and your review, so it stays in the local app.
        settings = AppSettings.model_validate({k: v for k, v in settings_doc.items() if k != "apply"})
    except ValueError as exc:
        raise PublishError(f"JOBBOT_CONFIG has an invalid profile or settings: {exc}") from exc

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


# -- encryption ----------------------------------------------------------------------------


def site_salt(site_id: str) -> bytes:
    """A fixed salt per site, so the key a browser remembers keeps working after each update."""
    return hashlib.sha256(f"jobbot-board:{site_id}".encode()).digest()[:16]


def derive_key(passphrase: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, dklen=32)


def encrypt(data: bytes, key: bytes) -> dict[str, str]:
    """AES-256-GCM. The browser decrypts this with WebCrypto."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    iv = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(iv, data, None)
    return {"iv": base64.b64encode(iv).decode(), "data": base64.b64encode(ciphertext).decode()}


def decrypt(blob: dict[str, str], key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key).decrypt(base64.b64decode(blob["iv"]), base64.b64decode(blob["data"]), None)


def save_state(db_path: Path, state_path: Path, key: bytes) -> None:
    """Encrypt a consistent copy of the database for the next run."""
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "state.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("VACUUM INTO ?", (str(snapshot),))
        finally:
            conn.close()
        blob = encrypt(gzip.compress(snapshot.read_bytes(), 6), key)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"v": 1, **blob}))


def restore_state(state_path: Path, db_path: Path, key: bytes) -> str:
    """Put the saved database back. Returns "restored", "missing" or "unreadable"."""
    if not state_path.is_file():
        return "missing"
    try:
        blob = json.loads(state_path.read_text())
        data = gzip.decompress(decrypt(blob, key))
    except Exception:
        return "unreadable"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    db_path.write_bytes(data)
    return "restored"


# -- the published board -------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    return value.isoformat(timespec="seconds") + "Z" if value else None


def _job(job: Job) -> dict[str, Any]:
    fit = job.fit or {}
    heuristic = job.heuristic or {}
    return {
        "id": job.fingerprint[:16],
        "title": job.title,
        "company": job.company,
        "location": job.location or "",
        "workplace": job.workplace or "",
        "type": job.employment_type or "",
        "salary": format_salary(job.salary_min, job.salary_max, job.salary_currency, job.salary_period),
        "posted": _iso(job.posted_at),
        "found": _iso(job.first_seen_at),
        "score": job.score,
        "ai": job.score_kind == "ai",
        "likelihood": fit.get("interview_likelihood"),
        "summary": fit.get("summary", ""),
        "strengths": fit.get("strengths", []),
        "gaps": fit.get("gaps", []),
        "dealbreakers": fit.get("dealbreakers", []),
        "tips": fit.get("tips", []),
        "seniority": fit.get("seniority_fit"),
        "skills": heuristic.get("matched_skills", []),
        "notes": heuristic.get("notes", []),
        "breakdown": {
            k: heuristic.get(k) for k in ("title", "skills", "experience", "freshness", "workplace")
        },
        "years": job.years_required,
        "url": job.url if is_web_url(job.url) else None,
        "apply": job.apply_url if is_web_url(job.apply_url) else None,
        "sources": sorted({source_label(e.get("source")) for e in job.sources or []})
        or [source_label(job.source)],
        "description": truncate(job.description or "", 8000),
    }


def board_payload(
    db: Database, scan_id: int | None = None, notices: list[str] | None = None
) -> dict[str, Any]:
    with db.session() as s:
        scan = s.get(ScanRun, scan_id) if scan_id else None
        if scan is None:
            scan = s.scalars(select(ScanRun).order_by(ScanRun.started_at.desc()).limit(1)).first()
        visible = or_(Job.hidden_reason.is_(None), Job.show_anyway.is_(True))
        current = (Job.closed_at.is_(None), Job.last_seen_at >= utcnow() - timedelta(days=STALE_DAYS))
        jobs = list(
            s.scalars(
                select(Job)
                .where(visible, *current)
                .order_by(Job.score.desc().nulls_last(), Job.posted_at.desc().nulls_last())
                .limit(MAX_JOBS)
            )
        )
        hidden = list(
            s.scalars(
                select(Job)
                .where(Job.hidden_reason.is_not(None), Job.show_anyway.is_(False), *current)
                .order_by(Job.first_seen_at.desc())
                .limit(MAX_HIDDEN)
            )
        )
        errors = [{"source": "Config", "error": n} for n in notices or []]
        errors += (scan.errors or []) if scan else []
        return {
            "generated": _iso(utcnow()),
            "scan": {
                "status": scan.status if scan else None,
                "started": _iso(scan.started_at) if scan else None,
                "stats": (scan.stats or {}) if scan else {},
                "errors": errors,
            },
            "jobs": [_job(j) for j in jobs],
            "hidden": [
                {
                    "id": j.fingerprint[:16],
                    "title": j.title,
                    "company": j.company,
                    "location": j.location or "",
                    "reason": j.hidden_reason,
                    "url": (j.apply_url or j.url) if is_web_url(j.apply_url or j.url) else None,
                }
                for j in hidden
            ],
        }


def pages_url(repository: str | None) -> str:
    """Where GitHub Pages serves a repository's site (unless it has a custom domain)."""
    if not repository or "/" not in repository:
        return ""
    owner, name = repository.split("/", 1)
    host = f"{owner.lower()}.github.io"
    return f"https://{host}/" if name.lower() == host else f"https://{host}/{name}/"


def setup_page(repository: str | None) -> str:
    """The public page shown until the board is configured. Contains nothing personal."""
    page = (ASSETS / "setup.html").read_text()
    repo_url = (
        f"https://github.com/{repository}" if repository else "https://github.com/Rusheekb/application-bot"
    )
    return page.replace("__REPO_URL__", html.escape(repo_url, quote=True))


def write_site(
    out: Path, payload: dict[str, Any] | None, key: bytes | None, salt: bytes, repository: str | None = None
) -> None:
    """Write index.html: the encrypted board, or setup instructions when not configured."""
    out.mkdir(parents=True, exist_ok=True)
    (out / ".nojekyll").write_text("")
    if payload is None or key is None:
        (out / "index.html").write_text(setup_page(repository))
        return
    raw = json.dumps(payload, separators=(",", ":")).encode()
    envelope = {
        "v": 1,
        "kdf": "PBKDF2-SHA256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": base64.b64encode(salt).decode(),
        "compression": "gzip",
        **encrypt(gzip.compress(raw, 9), key),
    }
    page = (ASSETS / "board.html").read_text()
    # JSON in a <script type="application/json"> block; escape "<" so it can't close the tag.
    data = json.dumps(envelope).replace("<", "\\u003c")
    (out / "index.html").write_text(page.replace("__JOBBOT_ENVELOPE__", data))


async def publish(
    env: Env,
    out: Path,
    state_path: Path,
    config_text: str | None,
    passphrase: str | None,
    board_url: str = "",
) -> int:
    """Run one hosted scan and write the site. Returns a process exit code.

    Output goes to a public log, so it only ever prints counts, never jobs, companies or searches."""
    repository = os.environ.get("GITHUB_REPOSITORY") or None
    salt = site_salt(repository or "jobbot")
    if not config_text or not passphrase:
        missing = [
            n for n, v in (("JOBBOT_CONFIG", config_text), ("JOBBOT_PAGE_PASSWORD", passphrase)) if not v
        ]
        print(f"Not set up yet ({' and '.join(missing)} missing): publishing the setup instructions page.")
        write_site(out, None, None, salt, repository)
        return 0
    if len(passphrase) < MIN_PASSPHRASE:
        raise PublishError(
            f"JOBBOT_PAGE_PASSWORD must be at least {MIN_PASSPHRASE} characters; use a long passphrase, "
            "since anyone can download the encrypted page and try to guess it"
        )
    config = decode_config(config_text)
    key = derive_key(passphrase, salt)

    env.ensure_dirs()
    restored = restore_state(state_path, env.db_path, key)
    print(
        {
            "restored": "Restored the job history from the last run.",
            "missing": "No saved job history yet: starting fresh.",
            "unreadable": "Couldn't decrypt the saved job history (was the passphrase changed?): starting fresh.",
        }[restored]
    )
    db = Database(env.db_path)
    try:
        db.create_all()
        warnings = await import_config(db, config)
        if warnings:
            print(f"{len(warnings)} config warning(s); the details are on your board.")

        from jobbot.pipeline import Scanner, rescore_all

        rescore_all(db)  # the profile or filters may have changed since the last run
        forget_old_jobs(db)
        scan_id = await Scanner(db, env, board_url=board_url or pages_url(repository)).run("schedule")
        payload = board_payload(db, scan_id, warnings)
        stats, errors = payload["scan"]["stats"], payload["scan"]["errors"]
        print(
            f"Scan {payload['scan']['status']}: {stats.get('fetched', 0)} jobs fetched, {stats.get('new', 0)} new, "
            f"{len(payload['jobs'])} on the board, {len(payload['hidden'])} hidden by your filters."
        )
        if problems := len(errors) - len(warnings):
            print(f"{problems} source(s) reported problems; the details are on your board.")
        write_site(out, payload, key, salt, repository)
    finally:
        db.engine.dispose()
    save_state(env.db_path, state_path, key)
    print("Published the encrypted board.")
    return 0
