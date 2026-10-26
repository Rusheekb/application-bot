"""Quick fill: filling applications from the profile with matching rules, no AI."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import select

from jobbot.apply.manager import ApplyError, ApplyManager
from jobbot.apply.quickfill import Answers, clean_label
from jobbot.db import Database, get_profile, get_settings, save_profile, save_resume, save_settings
from jobbot.models import ApplicationRun, Job
from jobbot.schemas import Education, Profile, QAPair
from tests.conftest import requires_browser

PROFILE = Profile(
    first_name="Jane",
    last_name="Doe",
    email="jane@example.com",
    phone="555-0100",
    linkedin_url="https://linkedin.com/in/janedoe",
    city="Austin",
    state="TX",
    country="United States",
    current_company="Initech",
    years_experience=4,
    work_authorization="US citizen",
    salary_expectation_text="$140k-$160k",
    how_did_you_hear="LinkedIn",
    education=[Education(school="UT Austin", degree="BS", field_of_study="Computer Science")],
)


def answer(label: str, kind: str = "text", options: list[str] | None = None, **profile) -> str | None:
    p = PROFILE.model_copy(update=profile)
    field = {"kind": kind, "label": label}
    if options is not None:
        field["options"] = [{"label": o} for o in options]
    return Answers(p, files={"resume"}, allow_consent=True).value_for(field)


@pytest.mark.parametrize(
    "label, expected",
    [
        ("First Name *", "Jane"),
        ("Last name", "Doe"),
        ("Name", "Jane Doe"),
        ("Full legal name", "Jane Doe"),
        ("Company name", None),
        ("Email address ✱", "jane@example.com"),
        ("Mobile phone", "555-0100"),
        ("LinkedIn Profile URL", "https://linkedin.com/in/janedoe"),
        ("Current location", "Austin, TX, United States"),
        ("Current company", "Initech"),
        ("How many years of experience do you have?", "4"),
        ("Years of experience with Spark", None),  # skill-specific: the candidate has to answer
        ("School", "UT Austin"),
        ("Field of study", "Computer Science"),
        ("What are your salary expectations?", "$140k-$160k"),
        ("How did you hear about this job?", "LinkedIn"),
        ("Why do you want to work here?", None),
        # Questions that merely mention a site or a contact detail.
        ("How did you hear about us? (e.g. LinkedIn, company website)", "LinkedIn"),
        ("What do you like about our website?", None),
        ("Do you have experience building mobile apps?", None),
        ("Location preference for this role", None),
    ],
)
def test_text_answers(label, expected):
    assert answer(label) == expected


def test_eligibility_answers_follow_the_profile():
    us = "Are you legally authorized to work in the United States?"
    assert answer(us, "radio", ["Yes", "No"]) == "Yes"
    assert answer("Are you authorized to work in Canada?", "radio", ["Yes", "No"]) is None  # another country
    assert answer(us, "radio", ["Yes", "No"], work_authorization="") is None  # profile doesn't say
    assert answer(us, "radio", ["Yes", "No"], work_authorization="Not authorized yet") is None

    sponsor = "Will you now or in the future require sponsorship for employment visa status?"
    assert answer(sponsor, "combobox") == "No"
    assert answer(sponsor, "combobox", requires_sponsorship=True) == "Yes"
    assert answer(sponsor, "combobox", work_authorization="") is None

    without = "Are you authorized to work in the US without sponsorship?"
    assert answer(without, "radio", ["Yes", "No"]) == "Yes"
    assert (
        answer(without, "radio", ["Yes", "No"], requires_sponsorship=True, work_authorization="H-1B") == "No"
    )

    assert (
        answer("Are you open to relocating to Seattle?", "radio", ["Yes", "No"]) is None
    )  # unset isn't "no"
    assert (
        answer("Are you open to relocating to Seattle?", "radio", ["Yes", "No"], willing_to_relocate=True)
        == "Yes"
    )
    assert answer("Do you hold an active security clearance?", "radio", ["Yes", "No"]) == "No"


def test_self_identification_declines_unless_the_profile_answers():
    options = ["Male", "Female", "I don't wish to answer"]
    assert answer("Gender", "select", options) == "I don't wish to answer"
    assert answer("Gender", "select", options, gender="Female") == "Female"
    assert answer(
        "Veteran status", "radio", ["I am a veteran", "I am not a veteran", "Decline to self-identify"]
    ) == ("Decline to self-identify")
    assert answer("Disability status", "text") is None  # no option to decline with: leave it empty


def test_files_consent_and_saved_answers():
    assert answer("Resume/CV *", "file") == "resume"
    assert answer("Cover Letter", "file") is None  # no cover letter for this job
    privacy = "I acknowledge that I have read the Candidate Privacy Notice *"
    assert answer(privacy, "checkbox") == "true"
    assert (
        Answers(PROFILE, files=set(), allow_consent=False).value_for({"kind": "checkbox", "label": privacy})
        is None
    )

    saved = PROFILE.model_copy(
        update={"qa": [QAPair(question="Do you have experience with Python?", answer="Yes, 4 years")]}
    )
    answers = Answers(saved, files=set(), allow_consent=True)
    assert (
        answers.value_for({"kind": "text", "label": "Do you have experience with Python? *"})
        == "Yes, 4 years"
    )
    assert answers.value_for({"kind": "text", "label": "Do you have experience with Java?"}) is None
    assert (
        clean_label("Years of experience with Spark (options: 1 | 2 | 3)") == "years of experience with spark"
    )


# -- end to end, in a browser ------------------------------------------------------------------


def _setup(db, tmp_path, **profile) -> None:
    resume = tmp_path / "Jane Doe Resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% test resume\n")
    with db.session() as s:
        save_profile(s, PROFILE.model_copy(update=profile))
        save_resume(s, {"filename": resume.name, "path": str(resume), "text": "Backend engineer."})
        settings = get_settings(s)
        settings.apply.enabled = True
        save_settings(s, settings)


def _job(db, url: str) -> int:
    with db.session() as s:
        job = Job(
            fingerprint=url,
            source="lever",
            url=url,
            apply_url=url,
            title="Engineer",
            company="Acme",
            sources=[],
        )
        s.add(job)
        s.flush()
        return job.id


def _run(db, run_id: int) -> ApplicationRun:
    with db.session() as s:
        return s.get(ApplicationRun, run_id)


@requires_browser
async def test_quick_fill_greenhouse_then_submit_after_approval(db, env, tmp_path, form_server, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # no key needed
    _setup(db, tmp_path)
    job_id = _job(db, f"{form_server}/greenhouse.html")
    manager = ApplyManager(db, env)
    try:
        run_id = await manager.enqueue(job_id, mode="review", engine="quick")
        await manager.wait(run_id)
        run = _run(db, run_id)
        assert run.status == "ready_for_review", (run.status, run.error, run.summary)
        answers = {a["label"].rstrip(" *"): a["value"] for a in run.answers}
        assert answers["First Name"] == "Jane" and answers["Email"] == "jane@example.com"
        assert answers["Resume/CV"] == "Jane Doe Resume.pdf"
        assert answers["Are you legally authorized to work in the United States?"] == "Yes"
        assert (
            answers["Will you now or in the future require sponsorship for employment visa status?"] == "No"
        )
        assert answers["Gender"] == "Decline To Self Identify"
        assert answers["I acknowledge that I have read the Candidate Privacy Notice"] == "Checked"
        assert answers["Why do you want to work at Acme?"] == ""  # open-ended: left for the candidate
        assert not await manager.live[run_id].session.page.evaluate("window.submitted || 0")
        with pytest.raises(ApplyError, match="written instructions"):
            await manager.revise(run_id, "Make it shorter")

        await manager.approve(run_id)
        await manager.wait(run_id)
        run = _run(db, run_id)
        assert run.status == "submitted", (run.status, run.error, run.summary)
        with db.session() as s:
            assert s.get(Job, job_id).status == "applied"
    finally:
        await manager.shutdown()


@requires_browser
async def test_quick_fill_asks_what_it_doesnt_know_and_remembers(db, env, tmp_path, form_server):
    _setup(db, tmp_path)
    manager = ApplyManager(db, env)
    try:
        run_id = await manager.enqueue(_job(db, f"{form_server}/lever.html"), mode="review", engine="quick")
        await manager.wait(run_id)
        run = _run(db, run_id)
        assert run.status == "needs_input" and run.missing_info == ["Years of experience with Spark"]
        answers = {a["label"].rstrip(" ✱"): a["value"] for a in run.answers}
        assert answers["Are you authorized to work in the US without sponsorship?"] == "Yes"
        assert answers["Veteran status"] == "Decline to self-identify"

        await manager.provide_answers(run_id, {"Years of experience with Spark": "3"}, remember=True)
        await manager.wait(run_id)
        run = _run(db, run_id)
        assert run.status == "ready_for_review", (run.status, run.missing_info)
        assert {a["label"].rstrip(" ✱"): a["value"] for a in run.answers}[
            "Years of experience with Spark"
        ] == "3"
        with db.session() as s:
            assert [(q.question, q.answer) for q in get_profile(s).qa] == [
                ("Years of experience with Spark", "3")
            ]

        # The next application with the same question doesn't need to ask.
        second = await manager.enqueue(
            _job(db, f"{form_server}/lever.html?again"), mode="review", engine="quick"
        )
        await manager.wait(second)
        assert _run(db, second).status == "ready_for_review"
    finally:
        await manager.shutdown()


@requires_browser
async def test_quick_fill_answers_button_questions(db, env, tmp_path, form_server):
    _setup(db, tmp_path, willing_to_relocate=True)
    manager = ApplyManager(db, env)
    try:
        run_id = await manager.enqueue(_job(db, f"{form_server}/ashby.html"), mode="review", engine="quick")
        await manager.wait(run_id)
        run = _run(db, run_id)
        assert run.status == "ready_for_review", (run.status, run.error, run.summary)
        assert "Not answered" not in run.summary
        page = manager.live[run_id].session.page
        assert "selected" in (await page.locator("button", has_text="Yes").get_attribute("class") or "")
    finally:
        await manager.shutdown()


async def test_quick_fill_route_needs_no_api_key(db, env, monkeypatch):
    from fastapi.testclient import TestClient

    from jobbot.web.app import create_app

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with db.session() as s:
        settings = get_settings(s)
        settings.apply.enabled = True
        save_settings(s, settings)
    job_id = _job(db, "https://jobs.lever.co/acme/1")
    queued = []

    async def fake_enqueue(job_id, **kwargs):
        queued.append(kwargs)
        return 7

    with TestClient(create_app(env, db=db, scheduler=False)) as client:
        monkeypatch.setattr(client.app.state.tasks.apply, "enqueue", fake_enqueue)
        origin = {"Origin": "http://testserver"}
        page = client.get(f"/jobs/{job_id}")
        assert "Quick fill" in page.text and "Fill application for review" not in page.text
        agent = client.post(f"/jobs/{job_id}/apply", data={"mode": "review"}, headers=origin)
        assert agent.status_code == 400 and "Quick fill" in agent.text
        quick = client.post(
            f"/jobs/{job_id}/apply", data={"engine": "quick", "mode": "submit"}, headers=origin
        )
        assert quick.status_code == 204 and quick.headers["HX-Redirect"] == "/applications/7"
    assert queued == [{"mode": "review", "engine": "quick"}]  # Quick fill always asks before submitting


def test_older_databases_gain_new_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE application_runs (id INTEGER PRIMARY KEY, job_id INTEGER, mode VARCHAR(10), "
        "status VARCHAR(20))"
    )
    conn.execute("INSERT INTO application_runs (id, job_id, mode, status) VALUES (1, 1, 'review', 'failed')")
    conn.commit()
    conn.close()
    db = Database(path)
    db.create_all()
    with db.session() as s:
        run = s.scalars(select(ApplicationRun)).one()
        assert run.engine is None and run.status == "failed"
