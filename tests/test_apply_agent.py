"""The apply agent against local copies of real-world application form layouts, with a scripted
stand-in for Claude. These tests exercise the real browser automation (Playwright + Chromium)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobbot.ai.client import LLM
from jobbot.apply.browser import BrowserSession, best_option
from jobbot.apply.manager import ApplyManager
from jobbot.db import get_settings, save_profile, save_resume, save_settings
from jobbot.models import ApplicationRun, Job
from jobbot.schemas import AISettings, Profile
from tests.conftest import requires_browser
from tests.fakes import FakeAnthropic, FormBrain

ANSWERS = {
    "first name": "Jane",
    "last name": "Doe",
    "full name": "Jane Doe",
    "name": "Jane Doe",
    "email": "jane@example.com",
    "phone": "555-0100",
    "linkedin": "https://linkedin.com/in/janedoe",
    "resume": "resume",
    "authorized to work": "Yes",
    "sponsorship": "No",
    "why do you want": "I love building reliable data systems.",
    "how did you hear": "LinkedIn",
    "gender": "Decline",
    "privacy": "true",
    "offices": "Remote",
    "spark": "3",
    "relocat": "Yes",
    "salary": "$150,000",
}


def test_best_option_matching():
    labels = ["Yes", "No", "Decline To Self Identify", "I am not a veteran", "Company website"]
    assert best_option("yes", labels) == "Yes"
    assert best_option("Decline", labels) == "Decline To Self Identify"
    assert best_option("not a veteran", labels) == "I am not a veteran"
    assert best_option("company site", labels) == "Company website"
    assert best_option("Purple", labels) is None
    assert best_option("Yes", ["Yes, I am authorized", "No, I am not"]) == "Yes, I am authorized"


def _setup(db, env, tmp_path: Path, mode: str = "review") -> int:
    resume = tmp_path / "Jane Doe Resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% test resume\n")
    with db.session() as s:
        save_profile(s, Profile(first_name="Jane", last_name="Doe", email="jane@example.com"))
        save_resume(s, {"filename": resume.name, "path": str(resume), "text": "Data engineer, 4 years."})
        settings = get_settings(s)
        settings.apply.enabled = True
        settings.apply.mode = mode
        save_settings(s, settings)
    return 0


def _job(db, url: str) -> int:
    with db.session() as s:
        job = Job(
            fingerprint=url,
            source="lever",
            url=url,
            apply_url=url,
            title="Data Engineer",
            company="Acme",
            description="Build pipelines with Spark.",
            sources=[],
        )
        s.add(job)
        s.flush()
        return job.id


def _manager(db, env, brain) -> ApplyManager:
    return ApplyManager(db, env, llm_factory=lambda settings: LLM(AISettings(), client=FakeAnthropic(brain)))


async def _snapshot(url: str) -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(url)
        state = await BrowserSession(context, page, files={}, mode="review").snapshot()
        await browser.close()
    return state


@requires_browser
async def test_snapshot_reads_greenhouse_layout(form_server):
    state = await _snapshot(f"{form_server}/greenhouse.html")
    fields = {f["label"].rstrip(" *"): f for f in state["fields"]}
    assert fields["First Name"]["required"] and fields["First Name"]["kind"] == "text"
    assert fields["Resume/CV"]["kind"] == "file" and fields["Resume/CV"]["required"]
    assert fields["Cover Letter"]["kind"] == "file" and not fields["Cover Letter"]["required"]
    assert fields["Are you legally authorized to work in the United States?"]["kind"] == "combobox"
    assert fields["I acknowledge that I have read the Candidate Privacy Notice"]["kind"] == "checkbox"
    submit = [b for b in state["buttons"] if b.get("final_submit")]
    assert [b["label"] for b in submit] == ["Submit application"]


@requires_browser
async def test_snapshot_reads_lever_layout(form_server):
    state = await _snapshot(f"{form_server}/lever.html")
    fields = {f["label"].rstrip(" ✱"): f for f in state["fields"]}
    radio = fields["Are you authorized to work in the US without sponsorship?"]
    assert radio["kind"] == "radio" and [o["label"] for o in radio["options"]] == ["Yes", "No"]
    offices = fields["Which of our offices could you work from?"]
    assert offices["kind"] == "checkboxes" and len(offices["options"]) == 3
    assert fields["Gender"]["kind"] == "select"
    assert "Select ..." not in [o["label"] for o in fields["Gender"]["options"]]
    assert fields["Additional information"]["kind"] == "textarea"
    assert fields["Resume/CV"]["kind"] == "file"


@requires_browser
async def test_snapshot_detects_login_and_captcha(form_server):
    login = await _snapshot(f"{form_server}/login.html")
    assert login["login"] is True
    captcha = await _snapshot(f"{form_server}/captcha.html")
    assert any("visible challenge" in c for c in captcha["captcha"])


@requires_browser
async def test_review_run_fills_lever_form_then_submits_after_approval(db, env, tmp_path, form_server):
    _setup(db, env, tmp_path)
    job_id = _job(db, f"{form_server}/lever.html")
    brain = FormBrain(ANSWERS)
    manager = _manager(db, env, brain)
    try:
        run_id = await manager.enqueue(job_id)
        await manager.wait(run_id)
        with db.session() as s:
            run = s.get(ApplicationRun, run_id)
            assert run.status == "ready_for_review", (run.status, run.error, run.summary)
            answers = {a["label"].rstrip(" ✱"): a["value"] for a in run.answers}
            assert answers["Full name"] == "Jane Doe"
            assert answers["Email"] == "jane@example.com"
            assert answers["Are you authorized to work in the US without sponsorship?"] == "Yes"
            assert answers["Which of our offices could you work from?"] == "Remote"
            assert answers["Years of experience with Spark"] == "3"
            assert answers["Gender"] == "Decline to self-identify"
            assert answers["Resume/CV"].endswith(".pdf")
            assert run.screenshot and (env.screenshot_dir / run.screenshot).exists()
            assert run.generated_answers == ["Why do you want to work at Acme?"]
        live = manager.live[run_id]
        assert not await live.session.page.evaluate("window.submitted || 0")

        await manager.approve(run_id)
        await manager.wait(run_id)
        with db.session() as s:
            run = s.get(ApplicationRun, run_id)
            assert run.status == "submitted", (run.status, run.error, run.summary)
            assert s.get(Job, job_id).status == "applied"
        assert run_id not in manager.live
    finally:
        await manager.shutdown()


@requires_browser
async def test_review_mode_refuses_the_final_submit_button(db, env, tmp_path, form_server):
    _setup(db, env, tmp_path)
    job_id = _job(db, f"{form_server}/greenhouse.html")
    brain = FormBrain(ANSWERS, try_submit_in_review=True)
    manager = _manager(db, env, brain)
    try:
        run_id = await manager.enqueue(job_id)
        await manager.wait(run_id)
        assert "blocked: this looks like the button that submits the application" in brain.log[-1]
        with db.session() as s:
            run = s.get(ApplicationRun, run_id)
            assert run.status == "ready_for_review", (run.status, run.error)
            answers = {a["label"].rstrip(" *"): a["value"] for a in run.answers}
            assert answers["Are you legally authorized to work in the United States?"] == "Yes"
            assert (
                answers["Will you now or in the future require sponsorship for employment visa status?"]
                == "No"
            )
            assert answers["How did you hear about this job?"] == "LinkedIn"
            assert answers["Gender"] == "Decline To Self Identify"
            assert answers["I acknowledge that I have read the Candidate Privacy Notice"] == "Checked"
            assert answers["Resume/CV"].endswith(".pdf")
        page = manager.live[run_id].session.page
        assert not await page.evaluate("window.submitted || 0")
    finally:
        await manager.shutdown()


@requires_browser
async def test_submit_mode_handles_buttons_as_answers(db, env, tmp_path, form_server):
    _setup(db, env, tmp_path, mode="submit")
    job_id = _job(db, f"{form_server}/ashby.html")
    brain = FormBrain(ANSWERS)
    brain.mode = "submit"
    manager = _manager(db, env, brain)
    try:
        run_id = await manager.enqueue(job_id)
        await manager.wait(run_id)
        with db.session() as s:
            run = s.get(ApplicationRun, run_id)
            assert run.status == "submitted", (run.status, run.error, run.summary)
        # The yes/no question was answered by clicking its button before submitting.
        checked_state = brain.log[0]
        assert "'Yes' (under question 'Are you open to relocating to Seattle?') [selected]" in checked_state
    finally:
        await manager.shutdown()


@requires_browser
async def test_agent_follows_apply_link_from_posting(db, env, tmp_path, form_server):
    _setup(db, env, tmp_path)
    job_id = _job(db, f"{form_server}/posting.html")
    manager = _manager(db, env, FormBrain(ANSWERS))
    try:
        run_id = await manager.enqueue(job_id)
        await manager.wait(run_id)
        with db.session() as s:
            run = s.get(ApplicationRun, run_id)
            assert run.status == "ready_for_review", (run.status, run.error)
            assert run.final_url.endswith("/lever.html")
    finally:
        await manager.shutdown()


@requires_browser
@pytest.mark.parametrize("page, status", [("captcha.html", "blocked"), ("login.html", "blocked")])
async def test_agent_stops_at_captcha_and_login(db, env, tmp_path, form_server, page, status):
    _setup(db, env, tmp_path)
    job_id = _job(db, f"{form_server}/{page}")
    manager = _manager(db, env, FormBrain(ANSWERS))
    try:
        run_id = await manager.enqueue(job_id)
        await manager.wait(run_id)
        with db.session() as s:
            assert s.get(ApplicationRun, run_id).status == status
        assert run_id not in manager.live  # headless: blocked runs don't keep a browser open
    finally:
        await manager.shutdown()


async def test_run_fails_cleanly_without_profile(db, env, form_server):
    job_id = _job(db, f"{form_server}/lever.html")
    manager = _manager(db, env, FormBrain(ANSWERS))
    try:
        run_id = await manager.enqueue(job_id)
        await manager.wait(run_id)
        with db.session() as s:
            run = s.get(ApplicationRun, run_id)
            assert run.status == "failed"
            assert "name and email" in run.error
    finally:
        await manager.shutdown()
