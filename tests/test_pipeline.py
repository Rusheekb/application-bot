"""End-to-end scan: fetch, de-duplicate, filter, score, store, notify (network and Claude faked)."""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import respx
from sqlalchemy import select

from jobbot.ai.client import LLM
from jobbot.db import get_settings, save_profile, save_settings
from jobbot.models import Company, Job, ScanRun, SearchQuery
from jobbot.pipeline import Scanner, rescore_all
from jobbot.schemas import AISettings, FitAnalysis, Profile
from jobbot.sources.base import JobPosting
from tests.conftest import load_json
from tests.fakes import FakeAnthropic, response

PROFILE = Profile(
    first_name="Jane",
    last_name="Doe",
    email="jane@example.com",
    years_experience=3,
    target_titles=["Backend Engineer", "Software Engineer", "Data Engineer"],
    skills=["Python", "PostgreSQL", "AWS", "Spark", "SQL"],
    requires_sponsorship=True,
)


def linkedin_duplicate() -> JobPosting:
    """The Greenhouse 'Backend Engineer' job, as seen on LinkedIn through JobSpy."""
    return JobPosting(
        source="linkedin",
        source_id="li-1",
        title="Backend Engineer",
        company="Acme, Inc.",
        url="https://www.linkedin.com/jobs/view/1",
        location="New York, NY",
        description="Short LinkedIn copy.",
        posted_at=datetime(2026, 10, 22),
    )


def linkedin_only() -> JobPosting:
    return JobPosting(
        source="linkedin",
        source_id="li-2",
        title="Python Developer",
        company="Soylent",
        url="https://www.linkedin.com/jobs/view/2",
        apply_url="https://jobs.lever.co/soylent/abc",
        location="Remote",
        description="Python and AWS. 2+ years of experience.",
    )


async def fake_jobspy(query, sites):
    assert sites == ["linkedin"]
    return [linkedin_duplicate(), linkedin_only()], {}


def fit_for(kwargs) -> FitAnalysis:
    job_text = kwargs["messages"][0]["content"]
    score = 91 if "Backend Engineer" in job_text else 64
    return FitAnalysis(
        score=score,
        interview_likelihood="high" if score > 75 else "medium",
        summary="Strong overlap with your Python backend work.",
        strengths=["Python"],
        gaps=[],
        dealbreakers=[],
        seniority_fit="match",
        tips=["Lead with payments experience"],
    )


def setup_db(db) -> None:
    with db.session() as s:
        save_profile(s, PROFILE)
        settings = get_settings(s)
        settings.filters.max_years_gap = 2
        settings.filters.max_age_days = None
        settings.notify.webhook_url = "https://hooks.slack.com/services/T000/B000/XXX"
        settings.notify.min_score = 80
        save_settings(s, settings)
        s.add(Company(name="Acme", ats="greenhouse", slug="acme"))
        s.add(Company(name="Globex", ats="lever", slug="globex"))
        s.add(SearchQuery(keywords="backend engineer", location="New York", sources=["linkedin"]))


@respx.mock
async def test_scan_end_to_end(db, env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    setup_db(db)
    greenhouse_route = respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").respond(
        json=load_json("greenhouse_jobs.json")
    )
    respx.get("https://api.lever.co/v0/postings/globex").respond(json=load_json("lever_postings.json"))
    slack = respx.post("https://hooks.slack.com/services/T000/B000/XXX").respond(200)

    fake = FakeAnthropic(lambda kwargs: response(parsed=fit_for(kwargs)))
    llm = LLM(AISettings(), client=fake)
    async with httpx.AsyncClient() as http:
        scanner = Scanner(db, env, http=http, llm=llm, jobspy_search=fake_jobspy)
        scan_id = await scanner.run("test")

    with db.session() as s:
        scan = s.get(ScanRun, scan_id)
        assert scan.status == "ok", scan.errors
        jobs = {j.title: j for j in s.scalars(select(Job))}

        # Board jobs that match none of the target titles are skipped.
        assert "Account Executive" not in jobs
        assert "Frontend Engineer (Contract)" not in jobs
        assert scan.stats["irrelevant"] == 2

        # The LinkedIn copy of Acme's Backend Engineer job merged into the Greenhouse record.
        backend = jobs["Backend Engineer"]
        assert backend.source == "greenhouse"
        assert {e["source"] for e in backend.sources} == {"greenhouse", "linkedin"}
        assert backend.apply_url == "https://job-boards.greenhouse.io/acme/jobs/4001"
        assert backend.ats == "greenhouse"
        assert backend.score_kind == "ai" and backend.score == 91
        assert backend.fit["interview_likelihood"] == "high"
        assert (backend.salary_min, backend.salary_max) == (140000, 170000)

        # Hard filters hide jobs with the reason recorded.
        assert (
            jobs["Senior Staff Software Engineer, Infrastructure"].hidden_reason
            == "Asks for 10+ years (you have 3)"
        )
        assert jobs["Software Engineer, Payments"].hidden_reason == "Says it can't sponsor work visas"

        # LinkedIn-only job keeps the company's own ATS link for applying.
        python_dev = jobs["Python Developer"]
        assert python_dev.apply_url == "https://jobs.lever.co/soylent/abc"
        assert python_dev.ats == "lever"
        assert python_dev.workplace == "remote"

        # Lever job parsed and scored.
        assert jobs["Data Engineer"].workplace == "hybrid"
        assert jobs["Data Engineer"].score_kind == "ai"

    # Claude saw the candidate in a cached system block and each job in the user message.
    first_call = fake.messages.calls[0]
    assert first_call["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert "<candidate_profile>" in first_call["system"][1]["text"]
    assert "jane@example.com" not in json.dumps(first_call["system"])  # no contact details for scoring
    assert first_call["output_format"] is FitAnalysis
    assert first_call["fallbacks"] == "default"

    # Only the >= 80 job was pushed to Slack.
    payload = json.loads(slack.calls[0].request.content)
    assert "Backend Engineer @ Acme" in payload["text"]
    assert "Data Engineer" not in payload["text"]

    # Second scan: the backend job disappeared from Acme's board, so it's marked closed; nothing duplicates.
    data = load_json("greenhouse_jobs.json")
    data["jobs"] = [j for j in data["jobs"] if j["id"] != 4001]
    greenhouse_route.respond(json=data)
    async with httpx.AsyncClient() as http:
        scan_id = await Scanner(db, env, http=http, llm=llm, jobspy_search=fake_jobspy).run("test")
    with db.session() as s:
        scan = s.get(ScanRun, scan_id)
        assert scan.stats["new"] == 0
        assert scan.stats["closed"] == 1
        backend = s.scalars(select(Job).where(Job.title == "Backend Engineer")).one()
        assert backend.closed_at is not None
        assert s.scalar(select(Job.id).where(Job.title == "Backend Engineer").limit(1)) == backend.id
        assert len(list(s.scalars(select(Job)))) == 5


@respx.mock
async def test_scan_reports_source_errors_without_failing(db, env):
    with db.session() as s:
        save_profile(s, PROFILE)
        s.add(Company(name="Gone", ats="greenhouse", slug="gone"))
        s.add(Company(name="Globex", ats="lever", slug="globex"))
    respx.get("https://boards-api.greenhouse.io/v1/boards/gone/jobs").respond(404)
    respx.get("https://api.lever.co/v0/postings/globex").respond(json=load_json("lever_postings.json"))
    async with httpx.AsyncClient() as http:
        scan_id = await Scanner(db, env, http=http).run("test")
    with db.session() as s:
        scan = s.get(ScanRun, scan_id)
        assert scan.status == "partial"
        assert scan.errors == [{"source": "Gone (Greenhouse)", "error": "no Greenhouse board named 'gone'"}]
        assert scan.stats["new"] == 1  # Lever's Data Engineer (the frontend role isn't a target title)
        gone = s.scalars(select(Company).where(Company.slug == "gone")).one()
        assert gone.last_status.startswith("Error:")
        # Without an API key every job keeps its heuristic score.
        assert {j.score_kind for j in s.scalars(select(Job))} == {"heuristic"}


async def test_empty_setup_is_reported(db, env):
    scan_id = await Scanner(db, env).run("test")
    with db.session() as s:
        scan = s.get(ScanRun, scan_id)
        assert scan.status == "error"
        assert "Nothing to scan yet" in scan.errors[0]["error"]


@respx.mock
async def test_auto_apply_queues_high_scores_within_daily_limit(db, env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    setup_db(db)
    with db.session() as s:
        settings = get_settings(s)
        settings.apply.enabled = True
        settings.apply.auto_after_scan = True
        settings.apply.auto_min_score = 85
        settings.notify.webhook_url = ""
        save_settings(s, settings)
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").respond(
        json=load_json("greenhouse_jobs.json")
    )
    respx.get("https://api.lever.co/v0/postings/globex").respond(json=load_json("lever_postings.json"))
    queued: list[int] = []

    async def on_auto_apply(ids):
        queued.extend(ids)

    llm = LLM(AISettings(), client=FakeAnthropic(lambda kwargs: response(parsed=fit_for(kwargs))))
    async with httpx.AsyncClient() as http:
        await Scanner(
            db, env, http=http, llm=llm, jobspy_search=fake_jobspy, on_auto_apply=on_auto_apply
        ).run()
    with db.session() as s:
        titles = [s.get(Job, i).title for i in queued]
    assert titles == ["Backend Engineer"]  # the only AI score >= 85 on a supported ATS


def test_rescore_all_applies_new_filters(db):
    with db.session() as s:
        save_profile(s, PROFILE)
        s.add(
            Job(
                fingerprint="a",
                source="greenhouse",
                url="https://x.test/1",
                title="Senior Backend Engineer",
                company="Acme",
                description="Python",
                sources=[],
            )
        )
    with db.session() as s:
        settings = get_settings(s)
        settings.filters.title_exclude = ["senior"]
        save_settings(s, settings)
    assert rescore_all(db) == 1
    with db.session() as s:
        job = s.scalars(select(Job)).one()
        assert job.hidden_reason == "Title contains “senior”"
        assert job.score_kind == "heuristic" and job.score is not None


# -- regressions -----------------------------------------------------------------------------

GREENHOUSE_ACME = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
SMARTRECRUITERS_ACME = "https://api.smartrecruiters.com/v1/companies/acme/postings"


def greenhouse(*jobs) -> dict:
    return {
        "jobs": [
            {
                "id": job_id,
                "title": title,
                "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
                "location": {"name": location},
                "content": description,
                "updated_at": "2026-10-20T00:00:00Z",
            }
            for job_id, title, location, description in jobs
        ]
    }


async def no_jobspy(query, sites):
    return [], {}


def setup_acme(db, ats: str = "greenhouse", **profile) -> None:
    with db.session() as s:
        save_profile(
            s, Profile(target_titles=["Software Engineer", "Backend Engineer", "Data Engineer"], **profile)
        )
        settings = get_settings(s)
        settings.filters.max_age_days = None
        save_settings(s, settings)
        s.add(Company(name="Acme", ats=ats, slug="acme"))


async def scan(db, env, jobspy=no_jobspy) -> ScanRun:
    async with httpx.AsyncClient() as http:
        scan_id = await Scanner(db, env, http=http, jobspy_search=jobspy).run("test")
    with db.session() as s:
        return s.get(ScanRun, scan_id)


async def test_two_openings_with_the_same_title_stay_separate(db, env):
    setup_acme(db)
    payments = (101, "Software Engineer", "New York, NY", "Payments team.")
    search = (102, "Software Engineer", "New York, NY", "Search team.")
    with respx.mock:
        respx.get(GREENHOUSE_ACME).respond(json=greenhouse(payments, search))
        assert (await scan(db, env)).stats["new"] == 2
    with respx.mock:  # the payments opening was filled; search is still open
        respx.get(GREENHOUSE_ACME).respond(json=greenhouse(search))
        result = await scan(db, env)
        assert result.stats["new"] == 0 and result.stats["closed"] == 1
    with respx.mock:  # and stays that way on the next scan
        respx.get(GREENHOUSE_ACME).respond(json=greenhouse(search))
        await scan(db, env)
    with db.session() as s:
        jobs = {j.source_job_id: j for j in s.scalars(select(Job))}
        assert set(jobs) == {"101", "102"}
        assert jobs["101"].closed_at is not None
        assert jobs["102"].closed_at is None and jobs["102"].url.endswith("/102")


async def test_a_fuller_description_reapplies_filters(db, env):
    setup_acme(db, years_experience=3)
    with db.session() as s:
        s.add(SearchQuery(keywords="backend engineer", sources=["linkedin"]))

    async def linkedin(query, sites):
        return [
            JobPosting(
                source="linkedin",
                source_id="li-1",
                title="Backend Engineer",
                company="Acme",
                url="https://www.linkedin.com/jobs/view/1",
                location="New York, NY",
                description="Short copy.",
            )
        ], {}

    with respx.mock:
        respx.get(GREENHOUSE_ACME).respond(json=greenhouse())
        await scan(db, env, linkedin)
    with respx.mock:
        respx.get(GREENHOUSE_ACME).respond(
            json=greenhouse((7, "Backend Engineer", "New York, NY", "You have 10+ years of experience."))
        )
        await scan(db, env, linkedin)
    with db.session() as s:
        job = s.scalars(select(Job)).one()
        assert job.source == "greenhouse" and job.years_required == 10
        assert job.hidden_reason == "Asks for 10+ years (you have 3)"


async def test_descriptions_over_the_cap_are_fetched_on_later_scans(db, env, monkeypatch):
    import jobbot.pipeline as pipeline

    monkeypatch.setattr(pipeline, "MAX_DETAIL_FETCHES", 1)
    setup_acme(db, ats="smartrecruiters")
    listing = {
        "offset": 0,
        "limit": 100,
        "totalFound": 2,
        "content": [
            {
                "id": "1",
                "name": "Data Engineer",
                "location": {"city": "Austin"},
                "ref": f"{SMARTRECRUITERS_ACME}/1",
            },
            {
                "id": "2",
                "name": "Data Engineer II",
                "location": {"city": "Austin"},
                "ref": f"{SMARTRECRUITERS_ACME}/2",
            },
        ],
    }

    def detail(i: int) -> dict:
        return {
            "jobAd": {
                "sections": {"jobDescription": {"title": "About", "text": f"<p>Job {i} uses Spark.</p>"}}
            }
        }

    for _ in range(2):
        with respx.mock:
            respx.get(SMARTRECRUITERS_ACME).respond(json=listing)
            respx.get(f"{SMARTRECRUITERS_ACME}/1").respond(json=detail(1))
            respx.get(f"{SMARTRECRUITERS_ACME}/2").respond(json=detail(2))
            await scan(db, env)
    with db.session() as s:
        descriptions = sorted(j.description for j in s.scalars(select(Job)))
        assert ["Job 1 uses Spark." in descriptions[0], "Job 2 uses Spark." in descriptions[1]] == [
            True,
            True,
        ]


async def test_links_from_job_details_must_be_web_links(db, env):
    setup_acme(db, ats="smartrecruiters")
    with respx.mock:
        respx.get(SMARTRECRUITERS_ACME).respond(
            json={
                "totalFound": 1,
                "content": [{"id": "1", "name": "Data Engineer", "ref": f"{SMARTRECRUITERS_ACME}/1"}],
            }
        )
        respx.get(f"{SMARTRECRUITERS_ACME}/1").respond(
            json={
                "postingUrl": "https://jobs.smartrecruiters.com/acme/1",
                "applyUrl": "file:///etc/passwd",
                "jobAd": {"sections": {"jobDescription": {"text": "<p>Spark.</p>"}}},
            }
        )
        await scan(db, env)
    with db.session() as s:
        job = s.scalars(select(Job)).one()
        assert job.url == "https://jobs.smartrecruiters.com/acme/1" and job.apply_url is None


@respx.mock
async def test_notifications_cannot_ping_everyone():
    from jobbot.notify import send_webhook

    job = Job(
        id=1, title="@everyone <!channel> Engineer", company="Acme & Co", url="https://x.test/1", score=90
    )
    discord = respx.post("https://discord.com/api/webhooks/1/abc").respond(204)
    slack = respx.post("https://hooks.slack.com/services/T/B/X").respond(200)
    async with httpx.AsyncClient() as http:
        await send_webhook(
            http, "https://discord.com/api/webhooks/1/abc", [job], board_url="https://me.github.io/jobs/"
        )
        await send_webhook(http, "https://hooks.slack.com/services/T/B/X", [job])
    sent = json.loads(discord.calls[0].request.content)
    assert sent["allowed_mentions"] == {"parse": []}
    assert (
        "https://x.test/1" in sent["content"]
        and "Your job board: https://me.github.io/jobs/" in sent["content"]
    )
    text = json.loads(slack.calls[0].request.content)["text"]
    assert "&lt;!channel&gt;" in text and "Acme &amp; Co" in text and "<!channel>" not in text
