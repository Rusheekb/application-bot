"""The scan pipeline: fetch → keep relevant titles → de-duplicate → enrich → filter → score →
store → notify (→ optionally queue auto-apply)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import func, select

from jobbot.ai import LLM, ai_ready, describe_error
from jobbot.ai.context import candidate_block
from jobbot.ai.fit import analyze_many, apply_fit
from jobbot.apply.support import AUTO_APPLY_ATS
from jobbot.config import Env
from jobbot.db import Database, get_profile, get_resume, get_settings
from jobbot.matching import heuristic_score, hidden_reason, is_relevant, relevance_terms
from jobbot.models import ApplicationRun, Company, Job, ScanRun, SearchQuery, utcnow
from jobbot.notify import send_webhook
from jobbot.schemas import AppSettings, Profile
from jobbot.sources import (
    API_SEARCH_SOURCES,
    COMPANY_SOURCES,
    JOBSPY_SITES,
    CompanyRef,
    JobPosting,
    SearchRef,
    SourceError,
    jobspy_source,
    priority,
    source_label,
)
from jobbot.sources.base import new_http_client, sanitize_posting
from jobbot.sources.detect import ats_for_url
from jobbot.text import (
    detect_seniority,
    detect_workplace,
    extract_years_required,
    job_fingerprint,
    parse_salary,
)

log = logging.getLogger(__name__)

MAX_DETAIL_FETCHES = 150
JobSpySearch = Callable[[SearchRef, list[str]], Awaitable[tuple[list[JobPosting], dict[str, str]]]]
AutoApplyHook = Callable[[list[int]], Awaitable[Any]]


@dataclass
class Found:
    posting: JobPosting
    origin: str
    company_id: int | None = None
    duplicates: list[Found] = field(default_factory=list)


def _chunks(items: Sequence[str], size: int = 400) -> Iterable[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _error_text(exc: Exception) -> str:
    text = str(exc) if isinstance(exc, SourceError) else f"{exc.__class__.__name__}: {exc}"
    return text if len(text) <= 300 else text[:297] + "…"


class Scanner:
    def __init__(
        self,
        db: Database,
        env: Env,
        *,
        http: httpx.AsyncClient | None = None,
        llm: LLM | None = None,
        jobspy_search: JobSpySearch | None = None,
        on_auto_apply: AutoApplyHook | None = None,
        board_url: str | None = None,
    ):
        self.db = db
        self.env = env
        self.http = http
        self.llm = llm
        self.jobspy_search = jobspy_search or jobspy_source.search
        self.on_auto_apply = on_auto_apply
        # Set for the hosted board: notifications then link to postings and the board, not the app.
        self.board_url = board_url

    # -- bookkeeping -----------------------------------------------------------------------

    def start(self, trigger: str) -> int:
        with self.db.session() as s:
            scan = ScanRun(trigger=trigger, status="running", progress="Starting")
            s.add(scan)
            s.flush()
            return scan.id

    def _progress(self, scan_id: int, text: str | None) -> None:
        with self.db.session() as s:
            scan = s.get(ScanRun, scan_id)
            if scan:
                scan.progress = text

    async def run(self, trigger: str = "manual", scan_id: int | None = None) -> int:
        scan_id = scan_id or self.start(trigger)
        try:
            stats, errors = await self._run(scan_id)
        except Exception as exc:
            log.exception("Scan %s failed", scan_id)
            with self.db.session() as s:
                scan = s.get(ScanRun, scan_id)
                scan.status = "error"
                scan.finished_at = utcnow()
                scan.progress = None
                scan.errors = [*(scan.errors or []), {"source": "scan", "error": _error_text(exc)}]
            return scan_id
        with self.db.session() as s:
            scan = s.get(ScanRun, scan_id)
            scan.finished_at = utcnow()
            scan.progress = None
            scan.stats = stats
            scan.errors = errors
            if not errors:
                scan.status = "ok"
            else:
                scan.status = "partial" if stats.get("fetched") else "error"
        return scan_id

    # -- the pipeline ------------------------------------------------------------------------

    async def _run(self, scan_id: int) -> tuple[dict[str, Any], list[dict[str, str]]]:
        with self.db.session() as s:
            profile = get_profile(s)
            settings = get_settings(s)
            resume_text = get_resume(s).get("text", "")
            companies = list(s.scalars(select(Company).where(Company.enabled)))
            searches = list(s.scalars(select(SearchQuery).where(SearchQuery.enabled)))

        stats: dict[str, Any] = dict.fromkeys(
            (
                "fetched",
                "irrelevant",
                "new",
                "updated",
                "hidden",
                "scored",
                "closed",
                "notified",
                "auto_apply",
            ),
            0,
        )
        errors: list[dict[str, str]] = []
        if not companies and not searches:
            errors.append(
                {
                    "source": "setup",
                    "error": "Nothing to scan yet: add companies or searches on the Sources page",
                }
            )
            return stats, errors

        terms = relevance_terms(settings.filters, profile, [q.keywords for q in searches])
        self._progress(
            scan_id,
            f"Checking {len(companies)} compan{'y' if len(companies) == 1 else 'ies'} and "
            f"{len(searches)} search{'' if len(searches) == 1 else 'es'}",
        )

        http = self.http or new_http_client()
        try:
            found, seen_by_company = await self._fetch_all(
                http, companies, searches, terms, settings, stats, errors
            )
            by_fingerprint = self._dedupe(found)

            with self.db.session() as s:
                known = self._existing_jobs(s, by_fingerprint)
            new_items = [(fp, f) for fp, f in by_fingerprint.items() if fp not in known]

            detail_needed = [f.posting for _, f in new_items if f.posting.needs_details]
            if detail_needed:
                self._progress(scan_id, f"Fetching details for {len(detail_needed)} new jobs")
                await self._fetch_details(http, detail_needed, errors)

            self._progress(scan_id, "Saving")
            new_ids = self._store(by_fingerprint, profile, settings, terms, stats)
            stats["closed"] = self._mark_closed(seen_by_company)

            if new_ids and ai_ready(settings.ai):
                await self._ai_score(scan_id, new_ids, profile, settings, resume_text, stats, errors)

            await self._notify(http, new_ids, settings, stats)
            await self._queue_auto_apply(new_ids, settings, stats)
        finally:
            if self.http is None:
                await http.aclose()
        return stats, errors

    async def _fetch_all(
        self,
        http: httpx.AsyncClient,
        companies: list[Company],
        searches: list[SearchQuery],
        terms: list[str],
        settings: AppSettings,
        stats: dict[str, Any],
        errors: list[dict[str, str]],
    ) -> tuple[list[Found], dict[int, set[str]]]:
        results: list[Found] = []
        seen_by_company: dict[int, set[str]] = {}
        limit = asyncio.Semaphore(6)
        scraper_lock = asyncio.Semaphore(1)  # one JobSpy search at a time to stay under rate limits

        async def fetch_company(company: Company) -> None:
            label = f"{company.name} ({source_label(company.ats)})"
            module = COMPANY_SOURCES.get(company.ats)
            if module is None:
                errors.append({"source": label, "error": f"unsupported job board type '{company.ats}'"})
                return
            ref = CompanyRef(
                company.ats,
                company.slug,
                company.name,
                dict(company.config or {}),
                search_terms=terms if company.ats == "workday" else [],
            )
            async with limit:
                try:
                    postings = await module.fetch_jobs(http, ref)
                except Exception as exc:
                    errors.append({"source": label, "error": _error_text(exc)})
                    self._record_company(company.id, f"Error: {_error_text(exc)}", None)
                    return
            postings = [p for p in postings if sanitize_posting(p)]
            relevant = [p for p in postings if is_relevant(p.title, terms)]
            stats["fetched"] += len(postings)
            stats["irrelevant"] += len(postings) - len(relevant)
            seen_by_company[company.id] = {p.source_id for p in postings}
            self._record_company(
                company.id,
                f"{len(postings)} open job{'s' if len(postings) != 1 else ''}, {len(relevant)} matching your titles",
                len(postings),
            )
            results.extend(Found(p, label, company_id=company.id) for p in relevant)

        async def run_search(query: SearchQuery) -> None:
            ref = SearchRef(
                keywords=query.keywords,
                location=query.location,
                remote_only=query.remote_only,
                results=query.results_per_source,
                hours_old=settings.jobspy_hours_old,
                country=settings.jobspy_country,
            )
            counts: list[str] = []
            sites = [s for s in query.sources if s in JOBSPY_SITES]
            if sites:
                async with scraper_lock:
                    try:
                        postings, site_errors = await self.jobspy_search(ref, sites)
                    except Exception as exc:
                        postings, site_errors = [], {}
                        names = ", ".join(JOBSPY_SITES[s] for s in sites)
                        errors.append({"source": f"{query.label} ({names})", "error": _error_text(exc)})
                for site, message in site_errors.items():
                    text = message if len(message) <= 300 else message[:297] + "…"
                    errors.append({"source": f"{query.label} ({source_label(site)})", "error": text})
                postings = [p for p in postings if sanitize_posting(p)]
                stats["fetched"] += len(postings)
                results.extend(
                    Found(p, f"Search: {query.label} ({source_label(p.source)})") for p in postings
                )
                counts.append(f"{len(postings)} from {', '.join(JOBSPY_SITES[s] for s in sites)}")
            for key in query.sources:
                module = API_SEARCH_SOURCES.get(key)
                if module is None:
                    continue
                if not module.configured(self.env):
                    errors.append(
                        {
                            "source": f"{query.label} ({module.LABEL})",
                            "error": f"{module.LABEL} isn't configured",
                        }
                    )
                    continue
                async with limit:
                    try:
                        postings = await module.search(http, ref, self.env)
                    except Exception as exc:
                        errors.append(
                            {"source": f"{query.label} ({module.LABEL})", "error": _error_text(exc)}
                        )
                        continue
                postings = [p for p in postings if sanitize_posting(p)]
                stats["fetched"] += len(postings)
                results.extend(Found(p, f"Search: {query.label} ({module.LABEL})") for p in postings)
                counts.append(f"{len(postings)} from {module.LABEL}")
            self._record_search(query.id, "; ".join(counts) or "no sources ran")

        await asyncio.gather(*(fetch_company(c) for c in companies), *(run_search(q) for q in searches))
        return results, seen_by_company

    def _record_company(self, company_id: int, status: str, count: int | None) -> None:
        with self.db.session() as s:
            company = s.get(Company, company_id)
            if company:
                company.last_checked_at = utcnow()
                company.last_status = status
                if count is not None:
                    company.last_job_count = count

    def _record_search(self, search_id: int, status: str) -> None:
        with self.db.session() as s:
            search = s.get(SearchQuery, search_id)
            if search:
                search.last_run_at = utcnow()
                search.last_status = status

    @staticmethod
    def _dedupe(found: list[Found]) -> dict[str, Found]:
        """Collapse the same job seen in several places, keeping the richest source as primary."""
        by_fp: dict[str, Found] = {}
        for item in found:
            p = item.posting
            fp = job_fingerprint(p.company, p.title, p.location)
            current = by_fp.get(fp)
            if current is None:
                by_fp[fp] = item
            elif priority(p.source) > priority(current.posting.source):
                item.duplicates = [current, *current.duplicates]
                current.duplicates = []
                by_fp[fp] = item
            else:
                current.duplicates.append(item)
        return by_fp

    @staticmethod
    def _existing_jobs(session: Any, by_fp: dict[str, Found]) -> dict[str, Job]:
        """Map fingerprint -> existing Job, matching by fingerprint or by posting URL."""
        known: dict[str, Job] = {}
        fps = list(by_fp)
        for chunk in _chunks(fps):
            for job in session.scalars(select(Job).where(Job.fingerprint.in_(chunk))):
                known[job.fingerprint] = job
        url_to_fp = {f.posting.url: fp for fp, f in by_fp.items() if fp not in known}
        for chunk in _chunks(list(url_to_fp)):
            for job in session.scalars(select(Job).where(Job.url.in_(chunk))):
                known[url_to_fp[job.url]] = job
        return known

    async def _fetch_details(
        self, http: httpx.AsyncClient, postings: list[JobPosting], errors: list[dict]
    ) -> None:
        limit = asyncio.Semaphore(6)
        failures: dict[str, int] = {}

        async def one(posting: JobPosting) -> None:
            module = COMPANY_SOURCES.get(posting.source)
            if module is None or not hasattr(module, "fetch_details"):
                return
            async with limit:
                try:
                    await module.fetch_details(http, posting)
                except Exception as exc:
                    failures[posting.company] = failures.get(posting.company, 0) + 1
                    log.debug("Detail fetch failed for %s: %s", posting.url, exc)

        await asyncio.gather(*(one(p) for p in postings[:MAX_DETAIL_FETCHES]))
        for company, n in failures.items():
            errors.append({"source": company, "error": f"couldn't load the description of {n} job(s)"})

    def _store(
        self,
        by_fp: dict[str, Found],
        profile: Profile,
        settings: AppSettings,
        terms: list[str],
        stats: dict[str, Any],
    ) -> list[int]:
        now = utcnow()
        new_jobs: list[Job] = []
        with self.db.session() as s:
            known = self._existing_jobs(s, by_fp)
            for fp, item in by_fp.items():
                job = known.get(fp)
                if job is not None:
                    self._merge(job, item, now)
                    stats["updated"] += 1
                    continue
                job = self._new_job(fp, item, profile, settings, terms, now)
                s.add(job)
                new_jobs.append(job)
                stats["new"] += 1
                if job.hidden_reason:
                    stats["hidden"] += 1
            s.flush()
            return [j.id for j in new_jobs]

    @staticmethod
    def _source_entries(item: Found, now_iso: str, existing: list[dict] | None = None) -> list[dict]:
        entries = list(existing or [])
        urls = {e.get("url") for e in entries}
        for f in [item, *item.duplicates]:
            if f.posting.url not in urls:
                entries.append({"source": f.posting.source, "url": f.posting.url, "seen": now_iso})
                urls.add(f.posting.url)
        return entries

    def _new_job(
        self, fp: str, item: Found, profile: Profile, settings: AppSettings, terms: list[str], now: Any
    ) -> Job:
        p = item.posting
        # Use an apply link from a duplicate (e.g. the company's ATS link found via LinkedIn).
        apply_url = p.apply_url or next(
            (d.posting.apply_url for d in item.duplicates if d.posting.apply_url), None
        )
        job = Job(
            fingerprint=fp,
            source=p.source,
            source_job_id=p.source_id[:200],
            url=p.url,
            apply_url=apply_url,
            company_id=item.company_id,
            title=p.title[:300],
            company=(p.company or "Unknown")[:200],
            location=(p.location or "")[:300] or None,
            employment_type=(p.employment_type or "")[:60] or None,
            department=(p.department or "")[:200] or None,
            description=p.description or "",
            posted_at=p.posted_at,
            salary_min=p.salary_min,
            salary_max=p.salary_max,
            salary_currency=p.salary_currency,
            salary_period=p.salary_period,
            first_seen_at=now,
            last_seen_at=now,
            status="new",
            found_by=item.origin[:300],
            sources=self._source_entries(item, now.isoformat()),
        )
        if job.salary_min is None and job.salary_max is None and (parsed := parse_salary(job.description)):
            job.salary_min, job.salary_max, job.salary_currency, job.salary_period = parsed
        job.workplace = p.workplace or detect_workplace(p.title, p.location, p.description)
        job.ats = ats_for_url(job.apply_url or job.url) or (p.source if p.source in COMPANY_SOURCES else None)
        job.years_required = extract_years_required(job.description)
        job.seniority = detect_seniority(job.title)
        job.hidden_reason = hidden_reason(job, profile, settings.filters, now)
        job.score, job.heuristic = heuristic_score(job, profile, terms, now)
        job.score_kind = "heuristic"
        return job

    def _merge(self, job: Job, item: Found, now: Any) -> None:
        p = item.posting
        job.last_seen_at = now
        job.closed_at = None
        job.sources = self._source_entries(item, now.isoformat(), job.sources)
        if priority(p.source) > priority(job.source):
            job.source, job.source_job_id, job.url = p.source, p.source_id[:200], p.url
            if item.company_id:
                job.company_id = item.company_id
            if p.apply_url:
                job.apply_url = p.apply_url
            if p.description and len(p.description) > len(job.description or ""):
                job.description = p.description
                job.years_required = extract_years_required(p.description)
        if not job.description and p.description:
            job.description = p.description
            job.years_required = extract_years_required(p.description)
        if not job.apply_url and p.apply_url:
            job.apply_url = p.apply_url
        if job.salary_min is None and job.salary_max is None and (p.salary_min or p.salary_max):
            job.salary_min, job.salary_max = p.salary_min, p.salary_max
            job.salary_currency, job.salary_period = p.salary_currency, p.salary_period
        if job.posted_at is None and p.posted_at:
            job.posted_at = p.posted_at
        job.ats = ats_for_url(job.apply_url or job.url) or job.ats

    def _mark_closed(self, seen_by_company: dict[int, set[str]]) -> int:
        """Jobs that disappeared from a company board we just read successfully have closed."""
        closed = 0
        now = utcnow()
        with self.db.session() as s:
            for company_id, seen in seen_by_company.items():
                company = s.get(Company, company_id)
                if company is None or company.ats == "workday":  # Workday is searched, not listed in full
                    continue
                stale = s.scalars(
                    select(Job).where(
                        Job.company_id == company_id, Job.source == company.ats, Job.closed_at.is_(None)
                    )
                )
                for job in stale:
                    if job.source_job_id not in seen:
                        job.closed_at = now
                        closed += 1
        return closed

    async def _ai_score(
        self,
        scan_id: int,
        new_ids: list[int],
        profile: Profile,
        settings: AppSettings,
        resume_text: str,
        stats: dict[str, Any],
        errors: list[dict[str, str]],
    ) -> None:
        with self.db.session() as s:
            jobs = [j for j in s.scalars(select(Job).where(Job.id.in_(new_ids))) if not j.is_hidden]
        jobs = [j for j in jobs if (j.score or 0) >= settings.ai.min_heuristic_score]
        jobs.sort(key=lambda j: j.score or 0, reverse=True)
        jobs = jobs[: max(0, settings.ai.max_scores_per_scan)]
        if not jobs:
            return
        try:
            llm = self.llm or LLM.from_settings(settings.ai)
        except Exception as exc:
            errors.append({"source": "AI scoring", "error": describe_error(exc)})
            return
        self._progress(scan_id, f"Analyzing {len(jobs)} jobs with Claude")
        failures: dict[str, int] = {}

        def save(job: Job, fit: Any, exc: Exception | None) -> None:
            with self.db.session() as s:
                row = s.get(Job, job.id)
                if row is None:
                    return
                if fit is not None:
                    apply_fit(row, fit, llm.model)
                    stats["scored"] += 1
                else:
                    message = describe_error(exc) if exc else "unknown error"
                    row.fit_error = message
                    failures[message] = failures.get(message, 0) + 1

        try:
            await analyze_many(llm, jobs, candidate_block(profile, resume_text), save)
        finally:
            if self.llm is None:  # created here, so close its HTTP client
                await llm.aclose()
        for message, n in failures.items():
            errors.append({"source": "AI scoring", "error": f"{message} ({n} job{'s' if n != 1 else ''})"})
        stats["ai_usage"] = llm.usage.as_dict(llm.model)

    async def _notify(
        self, http: httpx.AsyncClient, new_ids: list[int], settings: AppSettings, stats: dict
    ) -> None:
        if not settings.notify.webhook_url or not new_ids:
            return
        with self.db.session() as s:
            jobs = [
                j
                for j in s.scalars(select(Job).where(Job.id.in_(new_ids)))
                if not j.is_hidden and (j.score or 0) >= settings.notify.min_score
            ]
        jobs.sort(key=lambda j: j.score or 0, reverse=True)
        if not jobs:
            return
        if self.board_url is not None:
            base_url, board_url = "", self.board_url
        else:
            base_url, board_url = self.env.base_url or f"http://{self.env.host}:{self.env.port}", ""
        try:
            await send_webhook(http, settings.notify.webhook_url, jobs, base_url, board_url)
            stats["notified"] = len(jobs)
        except httpx.HTTPError as exc:
            log.warning("Notification failed: %s", exc)

    async def _queue_auto_apply(self, new_ids: list[int], settings: AppSettings, stats: dict) -> None:
        apply = settings.apply
        if not (self.on_auto_apply and apply.enabled and apply.auto_after_scan and new_ids):
            return
        if not ai_ready(settings.ai):
            return
        since = utcnow() - timedelta(days=1)
        with self.db.session() as s:
            used = s.scalar(
                select(func.count(ApplicationRun.id)).where(
                    ApplicationRun.trigger == "auto", ApplicationRun.created_at >= since
                )
            )
            remaining = max(0, apply.daily_limit - (used or 0))
            candidates = [
                j
                for j in s.scalars(select(Job).where(Job.id.in_(new_ids)))
                if not j.is_hidden
                and j.score_kind == "ai"
                and (j.score or 0) >= apply.auto_min_score
                and j.ats in AUTO_APPLY_ATS
            ]
        candidates.sort(key=lambda j: j.score or 0, reverse=True)
        chosen = [j.id for j in candidates[:remaining]]
        if chosen:
            await self.on_auto_apply(chosen)
            stats["auto_apply"] = len(chosen)


def rescore_all(db: Database, now: Any = None) -> int:
    """Re-run filters and the heuristic score for every job (after profile or filter changes).
    AI scores are kept; only heuristic scores are replaced."""
    with db.session() as s:
        profile = get_profile(s)
        settings = get_settings(s)
        searches = list(s.scalars(select(SearchQuery).where(SearchQuery.enabled)))
        terms = relevance_terms(settings.filters, profile, [q.keywords for q in searches])
        count = 0
        for job in s.scalars(select(Job)):
            job.hidden_reason = hidden_reason(job, profile, settings.filters, now)
            score, breakdown = heuristic_score(job, profile, terms, now)
            job.heuristic = breakdown
            if job.score_kind != "ai":
                job.score = score
                job.score_kind = "heuristic"
            count += 1
        return count
