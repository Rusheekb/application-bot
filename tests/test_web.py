"""Web UI routes and security middleware."""

from __future__ import annotations

import base64

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from jobbot.config import Env
from jobbot.db import get_profile, get_resume, get_settings, save_profile
from jobbot.models import ApplicationRun, Company, Job, SearchQuery
from jobbot.schemas import Profile
from jobbot.web.app import create_app
from tests.conftest import load_json

SAME_ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def app(env, db):
    return create_app(env, db=db, scheduler=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def add_job(db, **overrides) -> int:
    data = dict(
        fingerprint=overrides.pop("fingerprint", "fp-1"),
        source="greenhouse",
        url="https://job-boards.greenhouse.io/acme/jobs/1",
        apply_url="https://job-boards.greenhouse.io/acme/jobs/1",
        ats="greenhouse",
        title="Backend Engineer",
        company="Acme",
        location="New York, NY",
        description="Python and AWS.\n\n• 3+ years of experience",
        score=82,
        score_kind="ai",
        fit={
            "score": 82,
            "interview_likelihood": "high",
            "summary": "Your Python services work lines up well.",
            "strengths": ["Python"],
            "gaps": ["Kafka"],
            "dealbreakers": [],
            "seniority_fit": "match",
            "tips": ["Lead with payments work"],
        },
        heuristic={
            "title": 30,
            "skills": 20,
            "experience": 20,
            "freshness": 8,
            "workplace": 5,
            "matched_skills": ["Python"],
            "notes": [],
        },
        sources=[{"source": "greenhouse", "url": "https://job-boards.greenhouse.io/acme/jobs/1"}],
    )
    data.update(overrides)
    with db.session() as s:
        job = Job(**data)
        s.add(job)
        s.flush()
        return job.id


def test_empty_jobs_page_guides_setup(client):
    page = client.get("/jobs")
    assert page.status_code == 200
    assert "Finish setting up" in page.text
    assert "No jobs yet" in page.text


def test_jobs_page_lists_and_filters(client, db):
    add_job(db)
    add_job(
        db,
        fingerprint="fp-2",
        title="Senior Staff Engineer",
        hidden_reason="Asks for 10+ years (you have 3)",
        score=40,
    )
    add_job(
        db,
        fingerprint="fp-3",
        title="Frontend Engineer",
        status="saved",
        score=70,
        url="javascript:alert(1)",
        apply_url=None,
    )
    page = client.get("/jobs")
    assert "Backend Engineer" in page.text and "Your Python services work lines up well." in page.text
    assert "Senior Staff Engineer" not in page.text  # hidden
    hidden = client.get("/jobs?tab=hidden")
    assert "Senior Staff Engineer" in hidden.text and "Asks for 10+ years" in hidden.text
    saved = client.get("/jobs?tab=saved")
    assert "Frontend Engineer" in saved.text
    assert "javascript:alert" not in saved.text  # unsafe links never rendered
    assert "Backend Engineer" not in client.get("/jobs?q=frontend&tab=all").text
    assert "Backend Engineer" in client.get("/jobs?min_score=80").text
    assert "Backend Engineer" not in client.get("/jobs?min_score=90").text


def test_status_change_from_list_and_detail(client, db):
    job_id = add_job(db)
    removed = client.post(
        f"/jobs/{job_id}/status", data={"status": "saved", "tab": "new"}, headers=SAME_ORIGIN
    )
    assert removed.status_code == 200 and removed.text == ""
    assert "toast" in removed.headers["HX-Trigger"]
    kept = client.post(
        f"/jobs/{job_id}/status", data={"status": "applied", "tab": "all"}, headers=SAME_ORIGIN
    )
    assert f'id="job-{job_id}"' in kept.text
    detail = client.post(
        f"/jobs/{job_id}/status", data={"status": "interviewing", "tab": "detail"}, headers=SAME_ORIGIN
    )
    assert 'id="status-control"' in detail.text
    with db.session() as s:
        job = s.get(Job, job_id)
        assert job.status == "interviewing" and job.applied_at is not None
    assert (
        client.post(f"/jobs/{job_id}/status", data={"status": "bogus"}, headers=SAME_ORIGIN).status_code
        == 400
    )


def test_job_detail_page(client, db):
    job_id = add_job(db, notes="Referral from Sam")
    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200
    for text in (
        "Your chances",
        "High chance",
        "Kafka",
        "Referral from Sam",
        "• 3+ years of experience",
        "Apply on their site",
    ):
        assert text in page.text
    assert client.get("/jobs/999").status_code == 404
    client.post(f"/jobs/{job_id}/notes", data={"notes": "Call back Monday"}, headers=SAME_ORIGIN)
    with db.session() as s:
        assert s.get(Job, job_id).notes == "Call back Monday"


def test_analyze_without_api_key_shows_error(client, db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent")
    job_id = add_job(db)
    page = client.post(f"/jobs/{job_id}/analyze", headers=SAME_ORIGIN)
    assert "ANTHROPIC_API_KEY" in page.text


def test_profile_round_trip(client, db):
    form = {
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane@example.com",
        "years_experience": "4.5",
        "target_titles": "Backend Engineer\nSoftware Engineer, Platform",
        "skills": "Python, AWS\nPostgreSQL, python",
        "requires_sponsorship": "yes",
        "workplace_remote": "on",
        "desired_salary": "$150,000",
        "edu-1-school": "State University",
        "edu-1-degree": "BS",
        "qa-1-question": "Can you work on-site?",
        "qa-1-answer": "Yes, 3 days a week",
        "qa-2-question": "",
        "qa-2-answer": "dropped",
    }
    response = client.post("/profile", data=form, headers=SAME_ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    with db.session() as s:
        profile = get_profile(s)
    assert profile.full_name == "Jane Doe"
    assert profile.years_experience == 4.5
    assert profile.target_titles == ["Backend Engineer", "Software Engineer, Platform"]
    assert profile.skills == ["Python", "AWS", "PostgreSQL"]
    assert profile.requires_sponsorship and profile.workplace_preferences == ["remote"]
    assert profile.desired_salary == 150000
    assert profile.education[0].school == "State University"
    assert [(q.question, q.answer) for q in profile.qa] == [("Can you work on-site?", "Yes, 3 days a week")]
    page = client.get("/profile?msg=saved")
    assert "Profile saved" in page.text and "Software Engineer, Platform" in page.text


def test_resume_upload(client, db, env):
    resume = ("Jane Doe\nBackend engineer with 4 years of Python and AWS experience. " * 5).encode()
    response = client.post(
        "/profile/resume",
        files={"file": ("Jane Doe Resume.txt", resume, "text/plain")},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert response.headers["location"] == "/profile?msg=uploaded"
    with db.session() as s:
        stored = get_resume(s)
    assert stored["filename"] == "Jane Doe Resume.txt" and "Backend engineer" in stored["text"]
    assert client.get("/profile/resume/file").content == resume
    bad = client.post(
        "/profile/resume",
        files={"file": ("x.exe", b"MZ", "application/octet-stream")},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert "error=" in bad.headers["location"]


@respx.mock
def test_add_company_by_url_and_check(client, db):
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme").respond(
        json=load_json("greenhouse_board.json")
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").respond(
        json=load_json("greenhouse_jobs.json")
    )
    with db.session() as s:
        save_profile(s, Profile(target_titles=["Backend Engineer"]))
    response = client.post(
        "/companies",
        data={"url": "https://boards.greenhouse.io/acme"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert "Added+Acme+Corp" in response.headers["location"]
    duplicate = client.post(
        "/companies",
        data={"url": "https://job-boards.greenhouse.io/acme"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert "already+on+your+list" in duplicate.headers["location"]
    with db.session() as s:
        company = s.scalars(select(Company)).one()
    checked = client.post(f"/companies/{company.id}/check", headers=SAME_ORIGIN)
    assert "4 open jobs, 1 matching your titles" in checked.text
    unknown = client.post(
        "/companies", data={"url": "not a url"}, headers=SAME_ORIGIN, follow_redirects=False
    )
    assert "error=" in unknown.headers["location"]


def test_searches_and_filters(client, db):
    client.post(
        "/searches",
        data={
            "keywords": "backend engineer",
            "location": "New York",
            "sources": ["remotive", "bogus"],
            "results_per_source": "500",
        },
        headers=SAME_ORIGIN,
    )
    with db.session() as s:
        search = s.scalars(select(SearchQuery)).one()
    assert search.sources == ["remotive"] and search.results_per_source == 100
    client.post(f"/searches/{search.id}/toggle", headers=SAME_ORIGIN)
    client.post(
        "/filters",
        data={
            "title_exclude": "senior, staff\nprincipal",
            "min_salary": "$120,000",
            "max_years_gap": "",
            "include_remote": "on",
        },
        headers=SAME_ORIGIN,
    )
    with db.session() as s:
        assert not s.get(SearchQuery, search.id).enabled
        filters = get_settings(s).filters
    assert filters.title_exclude == ["senior", "staff", "principal"]
    assert filters.min_salary == 120000 and filters.max_years_gap is None and filters.include_remote
    page = client.get("/sources")
    assert "backend engineer" in page.text and "Paused" in page.text


def test_settings_save(client, db):
    response = client.post(
        "/settings",
        data={"scan_interval_hours": "12", "ai_enabled": "on", "model": "claude-haiku-5-5", "scoring_effort": "medium",
              "apply_enabled": "on", "apply_mode": "submit", "daily_limit": "5", "webhook_url": "https://ntfy.sh/me"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )  # fmt: skip
    assert response.status_code == 303
    with db.session() as s:
        settings = get_settings(s)
    assert settings.scan_interval_hours == 12
    assert settings.ai.model == "claude-haiku-5-5" and settings.ai.scoring_effort == "medium"
    assert settings.apply.enabled and settings.apply.mode == "submit" and settings.apply.daily_limit == 5
    assert not settings.apply.headless
    assert settings.notify.webhook_url == "https://ntfy.sh/me"
    assert "Integrations" in client.get("/settings").text


def test_application_pages(client, db, env):
    job_id = add_job(db)
    with db.session() as s:
        run = ApplicationRun(
            job_id=job_id,
            mode="review",
            status="ready_for_review",
            summary="Filled everything.",
            answers=[
                {"label": "Email *", "value": "jane@example.com", "kind": "email", "required": True},
                {
                    "label": "Why do you want to work at Acme?",
                    "value": "I love payments.",
                    "kind": "textarea",
                    "required": False,
                },
                {"label": "Phone", "value": "", "kind": "tel", "required": True},
            ],
            generated_answers=["Why do you want to work at Acme?"],
            steps=[{"t": "2026-10-25T10:00:00", "kind": "tool", "text": "get_page_state", "detail": ""}],
            screenshot="run_1.png",
        )
        s.add(run)
        s.flush()
        run_id = run.id
    (env.screenshot_dir / "run_1.png").write_bytes(b"\x89PNG fake")
    listing = client.get("/applications")
    assert "Ready for your review" in listing.text
    page = client.get(f"/applications/{run_id}")
    assert "written by AI" in page.text
    assert "The paused browser has closed" in page.text  # no live session in this test
    assert client.get(f"/applications/{run_id}/screenshot").content == b"\x89PNG fake"
    marked = client.post(f"/applications/{run_id}/mark-applied", headers=SAME_ORIGIN)
    assert marked.status_code == 204
    with db.session() as s:
        assert s.get(Job, job_id).status == "applied"


def test_apply_requires_feature_enabled(client, db):
    job_id = add_job(db)
    response = client.post(f"/jobs/{job_id}/apply", data={"mode": "review"}, headers=SAME_ORIGIN)
    assert response.status_code == 400 and "turned off" in response.text


def test_scan_status_and_history(client):
    status = client.get("/scan/status")
    assert "Scan now" in status.text and "No scans yet" in status.text
    assert "No scans yet" in client.get("/scans").text


# -- security ---------------------------------------------------------------------------------


def test_cross_site_posts_are_blocked(client, db):
    job_id = add_job(db)
    evil = client.post(
        f"/jobs/{job_id}/status", data={"status": "dismissed"}, headers={"Origin": "https://evil.example"}
    )
    assert evil.status_code == 403
    null_origin = client.post(
        f"/jobs/{job_id}/status", data={"status": "dismissed"}, headers={"Origin": "null"}
    )
    assert null_origin.status_code == 403
    referer = client.post(
        f"/jobs/{job_id}/status", data={"status": "saved"}, headers={"Referer": "https://evil.example/x"}
    )
    assert referer.status_code == 403
    with db.session() as s:
        assert s.get(Job, job_id).status == "new"


def test_unknown_host_is_rejected(client):
    response = client.get("/jobs", headers={"Host": "attacker.example"})
    assert response.status_code == 400


def test_password_protection(tmp_path, db):
    env = Env(data_dir=tmp_path / "data", password="hunter2")
    with TestClient(create_app(env, db=db, scheduler=False)) as c:
        assert c.get("/jobs").status_code == 401
        token = base64.b64encode(b"me:hunter2").decode()
        assert c.get("/jobs", headers={"Authorization": f"Basic {token}"}).status_code == 200
        wrong = base64.b64encode(b"me:nope").decode()
        assert c.get("/jobs", headers={"Authorization": f"Basic {wrong}"}).status_code == 401


def test_extra_allowed_host(tmp_path, db):
    env = Env(data_dir=tmp_path / "data", allowed_hosts="jobs.example.com")
    with TestClient(create_app(env, db=db, scheduler=False)) as c:
        assert c.get("/jobs", headers={"Host": "jobs.example.com"}).status_code == 200
