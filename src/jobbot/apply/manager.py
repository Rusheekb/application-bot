"""Runs apply agents in the background and keeps paused browser sessions alive for your review."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from jobbot.ai.client import LLM, describe_error
from jobbot.ai.context import application_profile_block
from jobbot.apply.agent import AgentOutcome, ApplyAgent, task_prompt
from jobbot.apply.browser import REVIEW_GUARD_JS, BrowserSession, answers_from_snapshot
from jobbot.config import Env
from jobbot.db import Database, get_profile, get_resume, get_settings, save_profile
from jobbot.models import ApplicationRun, Job, utcnow
from jobbot.schemas import AISettings, QAPair

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")
PAUSED = ("ready_for_review", "needs_input")
APPROVED_MESSAGE = (
    "The candidate reviewed the filled-in application and approved submitting it. You are now in submit "
    "mode: check the form once more with get_page_state, call submit_application on the final submit "
    "button, check the result, and call finish."
)


class ApplyError(Exception):
    """A problem that stops a run before the agent starts (shown to the user as-is)."""


@dataclass
class LiveRun:
    run_id: int
    agent: ApplyAgent
    session: BrowserSession
    context: Any
    expires_at: datetime
    headless: bool


def _safe_filename(text: str) -> str:
    return re.sub(r"[^\w .-]+", "", text).strip() or "Candidate"


class ApplyManager:
    def __init__(
        self,
        db: Database,
        env: Env,
        *,
        llm_factory: Callable[[AISettings], LLM] | None = None,
        max_concurrent: int = 2,
        max_live: int = 3,
        headless: bool | None = None,
        on_step: Callable[[int, dict], None] | None = None,
    ):
        self.db = db
        self.env = env
        self._llm_factory = llm_factory or LLM.from_settings
        self._headless_override = headless
        self._on_step = on_step
        self._slots = asyncio.Semaphore(max_concurrent)
        self.max_live = max_live
        self.live: dict[int, LiveRun] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        self._playwright: Any = None
        self._browsers: dict[bool, Any] = {}
        self._browser_lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None

    # -- public API ------------------------------------------------------------------------

    def is_live(self, run_id: int) -> bool:
        return run_id in self.live

    async def enqueue(
        self,
        job_id: int,
        *,
        mode: str | None = None,
        trigger: str = "manual",
        parent_run_id: int | None = None,
        previous_answers: list[dict] | None = None,
    ) -> int:
        with self.db.session() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise ApplyError("job not found")
            settings = get_settings(s)
            run = ApplicationRun(
                job_id=job_id,
                mode=mode or settings.apply.mode,
                trigger=trigger,
                status="queued",
                start_url=job.apply_url or job.url,
                parent_run_id=parent_run_id,
                steps=[],
            )
            s.add(run)
            s.flush()
            run_id = run.id
        self.tasks[run_id] = asyncio.create_task(self._execute(run_id, previous_answers))
        self._start_reaper()
        return run_id

    async def approve(self, run_id: int) -> int:
        """Submit a run that is waiting for review. Returns the id of the run doing the submission."""
        with self.db.session() as s:
            run = s.get(ApplicationRun, run_id)
            if run is None or run.status != "ready_for_review":
                raise ApplyError("this application isn't waiting for review")
            job_id, trigger, answers = run.job_id, run.trigger, list(run.answers or [])
        live = self.live.get(run_id)
        if live is not None:
            self._update(run_id, status="running", mode="submit")
            live.agent.set_mode("submit")
            self.tasks[run_id] = asyncio.create_task(self._continue(run_id, live, APPROVED_MESSAGE))
            return run_id
        # The paused browser session is gone (expired or restarted): redo it in submit mode.
        self._update(run_id, status="superseded")
        return await self.enqueue(
            job_id, mode="submit", trigger=trigger, parent_run_id=run_id, previous_answers=answers
        )

    async def revise(self, run_id: int, instruction: str) -> None:
        live = self._require_live(run_id)
        mode_note = " Don't submit." if live.agent.mode == "review" else ""
        message = (
            "The candidate reviewed your work and asks for these changes:\n"
            f"{instruction.strip()}\n\nMake the changes, check the form with get_page_state, then call finish "
            f"again.{mode_note}"
        )
        self._update(run_id, status="running")
        self.tasks[run_id] = asyncio.create_task(self._continue(run_id, live, message))

    async def provide_answers(self, run_id: int, answers: dict[str, str], remember: bool) -> None:
        live = self._require_live(run_id)
        answered = {q: a.strip() for q, a in answers.items() if a and a.strip()}
        if not answered:
            raise ApplyError("no answers given")
        if remember:
            with self.db.session() as s:
                profile = get_profile(s)
                known = {p.question.strip().lower() for p in profile.qa}
                for question, answer in answered.items():
                    if question.strip().lower() not in known:
                        profile.qa.append(QAPair(question=question.strip(), answer=answer))
                save_profile(s, profile)
        lines = "\n".join(f"Q: {q}\nA: {a}" for q, a in answered.items())
        message = (
            f"The candidate answered your questions:\n{lines}\n\nUse these answers to complete the form, "
            "check it with get_page_state, then call finish again."
        )
        self._update(run_id, status="running")
        self.tasks[run_id] = asyncio.create_task(self._continue(run_id, live, message))

    async def cancel(self, run_id: int) -> None:
        task = self.tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        await self._close_live(run_id)
        with self.db.session() as s:
            run = s.get(ApplicationRun, run_id)
            if run is not None and run.status in (*ACTIVE, *PAUSED, "blocked"):
                run.status = "cancelled"
                run.finished_at = utcnow()

    async def wait(self, run_id: int) -> None:
        task = self.tasks.get(run_id)
        if task is not None:
            await asyncio.shield(task)

    async def shutdown(self) -> None:
        for task in list(self.tasks.values()):
            task.cancel()
        for run_id in list(self.live):
            await self._close_live(run_id)
        if self._reaper:
            self._reaper.cancel()
        for browser in self._browsers.values():
            with contextlib.suppress(Exception):
                await browser.close()
        self._browsers.clear()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

    def recover_after_restart(self) -> None:
        """Runs that were in progress when the app stopped can't continue."""
        with self.db.session() as s:
            for run in s.scalars(select(ApplicationRun).where(ApplicationRun.status.in_(ACTIVE))):
                run.status = "failed"
                run.error = "Interrupted because jobbot restarted"
                run.finished_at = utcnow()

    # -- internals -----------------------------------------------------------------------------

    def _require_live(self, run_id: int) -> LiveRun:
        live = self.live.get(run_id)
        if live is None:
            raise ApplyError("the browser session for this run has closed; start a new run instead")
        if run_id in self.tasks and not self.tasks[run_id].done():
            raise ApplyError("the agent is already working on this run")
        return live

    def _update(self, run_id: int, **fields: Any) -> None:
        with self.db.session() as s:
            run = s.get(ApplicationRun, run_id)
            if run is not None:
                for key, value in fields.items():
                    setattr(run, key, value)

    def _append_step(self, run_id: int, entry: dict) -> None:
        with self.db.session() as s:
            run = s.get(ApplicationRun, run_id)
            if run is not None:
                run.steps = [*(run.steps or []), entry]
        if self._on_step is not None:
            self._on_step(run_id, entry)

    def _fail(self, run_id: int, message: str) -> None:
        self._update(run_id, status="failed", error=message, finished_at=utcnow())

    async def _execute(self, run_id: int, previous_answers: list[dict] | None) -> None:
        async with self._slots:
            try:
                await self._run_agent(run_id, previous_answers)
            except asyncio.CancelledError:
                self._update(run_id, status="cancelled", finished_at=utcnow())
                await self._close_live(run_id)
                raise
            except ApplyError as exc:
                self._fail(run_id, str(exc))
            except Exception as exc:
                log.exception("Apply run %s failed", run_id)
                self._fail(run_id, describe_error(exc))
                await self._close_live(run_id)
            finally:
                self.tasks.pop(run_id, None)

    async def _run_agent(self, run_id: int, previous_answers: list[dict] | None) -> None:
        with self.db.session() as s:
            run = s.get(ApplicationRun, run_id)
            job = s.get(Job, run.job_id)
            profile = get_profile(s)
            settings = get_settings(s)
            resume = get_resume(s)
            mode, start_url = run.mode, run.start_url or job.url
            job_snapshot = Job(
                id=job.id,
                title=job.title,
                company=job.company,
                location=job.location,
                url=job.url,
                description=job.description,
            )
            cover_letter = job.cover_letter

        llm = self._llm_factory(settings.ai)
        try:
            try:
                import playwright  # noqa: F401
            except ImportError as exc:
                raise ApplyError(
                    'Browser automation isn\'t installed. Run: pip install "jobbot[apply]" && playwright install chromium'
                ) from exc
            if not (profile.full_name and profile.email):
                raise ApplyError("Add at least your name and email on the Profile page first")
            files: dict[str, Path] = {}
            resume_path = Path(resume["path"]) if resume.get("path") else None
            if resume_path and resume_path.exists():
                files["resume"] = resume_path
            if cover_letter:
                files["cover_letter"] = self._write_cover_letter(
                    job_snapshot.id, profile.full_name, cover_letter
                )

            headless = settings.apply.headless if self._headless_override is None else self._headless_override
            self._update(run_id, status="running", started_at=utcnow())
            self._append_step(
                run_id,
                {"t": utcnow().isoformat(timespec="seconds"), "kind": "nav", "text": f"Opening {start_url}"},
            )
            browser = await self._browser(headless)
            context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
            try:
                if mode == "review":
                    await context.add_init_script(REVIEW_GUARD_JS)
                page = await context.new_page()
                try:
                    await page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
                except Exception as exc:
                    raise ApplyError(
                        f"Couldn't open the application page: {str(exc).splitlines()[0][:200]}"
                    ) from exc
                session = BrowserSession(context, page, files=files, mode=mode)
                agent = ApplyAgent(
                    llm,
                    session,
                    profile_text=application_profile_block(profile, resume.get("text", "")),
                    task=task_prompt(
                        job_snapshot,
                        mode=mode,
                        start_url=start_url,
                        allow_consent=settings.apply.allow_consent_checkboxes,
                        cover_letter_file="cover_letter" in files,
                        previous_answers=previous_answers,
                    ),
                    mode=mode,
                    effort=settings.ai.agent_effort,
                    max_steps=settings.apply.max_steps,
                    on_step=lambda entry: self._append_step(run_id, entry),
                )
                live = LiveRun(run_id, agent, session, context, utcnow(), headless)
                self.live[run_id] = live  # registered so cancel() can close it mid-run
                outcome = await agent.start()
            except BaseException:
                self.live.pop(run_id, None)
                await context.close()
                raise
        except BaseException:
            await llm.aclose()
            raise
        await self._finish_round(run_id, outcome, live)

    async def _continue(self, run_id: int, live: LiveRun, message: str) -> None:
        async with self._slots:
            try:
                live.expires_at = utcnow() + timedelta(days=1)
                outcome = await live.agent.resume(message)
                await self._finish_round(run_id, outcome, live)
            except asyncio.CancelledError:
                self._update(run_id, status="cancelled", finished_at=utcnow())
                await self._close_live(run_id)
                raise
            except Exception as exc:
                log.exception("Apply run %s failed", run_id)
                self._fail(run_id, describe_error(exc))
                await self._close_live(run_id)
            finally:
                self.tasks.pop(run_id, None)

    async def _finish_round(self, run_id: int, outcome: AgentOutcome, live: LiveRun) -> None:
        session = live.session
        answers: list[dict] = []
        try:
            state = await session.snapshot()
            answers = answers_from_snapshot(state)
        except Exception as exc:
            log.debug("Final snapshot failed: %s", exc)
        shot = f"run_{run_id}_{int(time.time())}.png"
        saved = await session.save_screenshot(self.env.screenshot_dir / shot)
        with self.db.session() as s:
            run = s.get(ApplicationRun, run_id)
            settings = get_settings(s)
            run.status = outcome.status
            run.summary = outcome.summary
            run.missing_info = outcome.missing_info
            run.generated_answers = outcome.generated_answers
            run.final_url = session.page.url
            run.finished_at = utcnow()
            run.usage = live.agent.llm.usage.as_dict(live.agent.llm.model)
            if saved:
                run.screenshot = shot
            if answers and any(a["value"] for a in answers):
                run.answers = answers
            if outcome.status == "submitted":
                job = s.get(Job, run.job_id)
                now = utcnow()
                job.status = "applied"
                job.applied_at = now
                job.status_changed_at = now
            minutes = settings.apply.live_session_minutes
        keep = outcome.status in PAUSED or (outcome.status == "blocked" and not live.headless)
        if keep:
            live.expires_at = utcnow() + timedelta(minutes=minutes)
            self.live[run_id] = live
            await self._enforce_live_limit()
        else:
            await self._close_live(run_id, live)

    async def _close_live(self, run_id: int, live: LiveRun | None = None) -> None:
        live = self.live.pop(run_id, None) or live
        if live is not None:
            with contextlib.suppress(Exception):
                await live.context.close()

    async def _enforce_live_limit(self) -> None:
        while len(self.live) > self.max_live:
            oldest = min(self.live.values(), key=lambda lr: lr.expires_at)
            await self._close_live(oldest.run_id)

    def _start_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap())

    async def _reap(self) -> None:
        idle_since: float | None = None
        while True:
            await asyncio.sleep(30)
            now = utcnow()
            for run_id, live in list(self.live.items()):
                if live.expires_at <= now and not (run_id in self.tasks and not self.tasks[run_id].done()):
                    await self._close_live(run_id)
            if self.live or self.tasks:
                idle_since = None
            elif idle_since is None:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since > 600 and self._browsers:
                for browser in self._browsers.values():
                    with contextlib.suppress(Exception):
                        await browser.close()
                self._browsers.clear()

    async def _browser(self, headless: bool) -> Any:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        async with self._browser_lock:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            browser = self._browsers.get(headless)
            if browser is not None and browser.is_connected():
                return browser
            kwargs: dict[str, Any] = {"headless": headless}
            if self.env.chromium_path:
                kwargs["executable_path"] = self.env.chromium_path
            try:
                browser = await self._playwright.chromium.launch(**kwargs)
            except PlaywrightError as exc:
                message = str(exc)
                if "Executable doesn't exist" in message:
                    raise ApplyError(
                        "Chromium isn't installed for Playwright. Run: playwright install chromium"
                    ) from exc
                if not headless:
                    raise ApplyError(
                        "Couldn't open a visible browser window (is there a display?). Turn on headless mode in Settings."
                    ) from exc
                raise
            self._browsers[headless] = browser
            return browser

    def _write_cover_letter(self, job_id: int, name: str, text: str) -> Path:
        import docx

        folder = self.env.documents_dir / f"job_{job_id}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{_safe_filename(name)} - Cover Letter.docx"
        document = docx.Document()
        for paragraph in re.split(r"\n\s*\n", text.strip()):
            document.add_paragraph(paragraph.strip())
        document.save(str(path))
        return path


def run_is_live(manager: ApplyManager | None, run: ApplicationRun) -> bool:
    return bool(manager and manager.is_live(run.id))


def describe_status(status: str) -> str:
    return {
        "queued": "Queued",
        "running": "Working…",
        "ready_for_review": "Ready for your review",
        "needs_input": "Needs your input",
        "blocked": "Blocked",
        "submitted": "Submitted",
        "failed": "Failed",
        "cancelled": "Cancelled",
        "superseded": "Replaced by a new run",
    }.get(status, status)
