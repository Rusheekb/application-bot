"""Source connectors parse each API's response format (HTTP mocked with respx)."""

from __future__ import annotations

import json
import math
from datetime import datetime

import httpx
import pytest
import respx

from jobbot.config import Env
from jobbot.sources import (
    adzuna,
    ashby,
    greenhouse,
    jobspy_source,
    jsearch,
    lever,
    recruitee,
    remoteok,
    remotive,
    smartrecruiters,
    workable,
    workday,
)
from jobbot.sources.base import CompanyRef, SearchRef, SourceError, request_json, sanitize_posting
from jobbot.sources.detect import ats_for_url, find_ats_in_html, parse_ats_url
from tests.conftest import load_json


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@respx.mock
async def test_greenhouse(client):
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").respond(
        json=load_json("greenhouse_jobs.json")
    )
    jobs = await greenhouse.fetch_jobs(client, CompanyRef("greenhouse", "acme", "Acme"))
    assert len(jobs) == 4
    job = jobs[0]
    assert job.title == "Backend Engineer"
    assert job.company == "Acme"
    assert job.location == "New York, NY"
    assert job.apply_url == "https://job-boards.greenhouse.io/acme/jobs/4001"
    assert job.department == "Engineering"
    assert "• 3+ years of experience building services in Python" in job.description
    assert "<" not in job.description
    assert (job.salary_min, job.salary_max, job.salary_period) == (140000, 170000, "year")
    assert job.posted_at == datetime(2026, 10, 21, 13, 0)
    # A company careers page in absolute_url still gets the Greenhouse-hosted form to apply on.
    assert jobs[1].url == "https://acme.com/careers?gh_jid=4002"
    assert jobs[1].apply_url == "https://job-boards.greenhouse.io/acme/jobs/4002"


@respx.mock
async def test_greenhouse_errors(client):
    respx.get("https://boards-api.greenhouse.io/v1/boards/nope/jobs").respond(404)
    with pytest.raises(SourceError, match="no Greenhouse board named 'nope'"):
        await greenhouse.fetch_jobs(client, CompanyRef("greenhouse", "nope"))


@respx.mock
async def test_greenhouse_board_name_and_eu_region(client):
    respx.get("https://boards-api.eu.greenhouse.io/v1/boards/acme").respond(
        json=load_json("greenhouse_board.json")
    )
    name = await greenhouse.fetch_board_name(
        client, CompanyRef("greenhouse", "acme", config={"region": "eu"})
    )
    assert name == "Acme Corp"


@respx.mock
async def test_lever(client):
    route = respx.get("https://api.lever.co/v0/postings/globex").respond(
        json=load_json("lever_postings.json")
    )
    jobs = await lever.fetch_jobs(client, CompanyRef("lever", "globex", "Globex"))
    assert route.calls[0].request.url.params["mode"] == "json"
    data, frontend = jobs
    assert data.title == "Data Engineer"
    assert data.workplace == "hybrid"
    assert data.location == "Austin, TX"
    assert data.employment_type == "Full-time"
    assert (data.salary_min, data.salary_max, data.salary_currency, data.salary_period) == (
        120000,
        150000,
        "USD",
        "year",
    )
    assert "Requirements\n• 2-4 years of experience with Spark" in data.description
    assert data.apply_url.endswith("/apply")
    assert frontend.workplace == "remote"
    assert (frontend.salary_min, frontend.salary_max, frontend.salary_period) == (60, 75, "hour")


@respx.mock
async def test_lever_eu(client):
    respx.get("https://api.eu.lever.co/v0/postings/globex").respond(json=[])
    assert await lever.fetch_jobs(client, CompanyRef("lever", "globex", config={"region": "eu"})) == []


@respx.mock
async def test_ashby(client):
    respx.get("https://api.ashbyhq.com/posting-api/job-board/initech").respond(
        json=load_json("ashby_board.json")
    )
    jobs = await ashby.fetch_jobs(client, CompanyRef("ashby", "initech", "Initech"))
    assert len(jobs) == 1  # unlisted job skipped
    job = jobs[0]
    assert job.location == "Seattle, WA; Remote (US)"
    assert job.workplace == "hybrid"
    assert (job.salary_min, job.salary_max, job.salary_currency) == (150000, 190000, "USD")
    assert job.apply_url.endswith("/application")
    assert job.employment_type == "FullTime"


@respx.mock
async def test_smartrecruiters_with_details(client):
    respx.get("https://api.smartrecruiters.com/v1/companies/Umbrella/postings").respond(
        json=load_json("smartrecruiters_postings.json")
    )
    respx.get("https://api.smartrecruiters.com/v1/companies/Umbrella/postings/744000098765432").respond(
        json=load_json("smartrecruiters_detail.json")
    )
    jobs = await smartrecruiters.fetch_jobs(client, CompanyRef("smartrecruiters", "Umbrella"))
    job = jobs[0]
    assert job.company == "Umbrella Corporation"
    assert job.location == "Raleigh, NC, United States"
    assert job.needs_details and not job.description
    await smartrecruiters.fetch_details(client, job)
    assert "Build lab software in Java." in job.description
    assert "3+ years of experience with Java" in job.description
    assert job.apply_url.startswith("https://jobs.smartrecruiters.com/oneclick-ui/")
    assert (job.salary_min, job.salary_max) == (110000, 135000)


@respx.mock
async def test_workable(client):
    respx.get("https://apply.workable.com/api/v1/widget/accounts/hooli").respond(
        json=load_json("workable_widget.json")
    )
    jobs = await workable.fetch_jobs(client, CompanyRef("workable", "hooli"))
    job = jobs[0]
    assert job.company == "Hooli"
    assert job.location == "Palo Alto, California, United States"
    assert job.workplace == "remote"
    assert job.apply_url == "https://apply.workable.com/j/9A8B7C6D5E/apply"
    assert not job.needs_details


@respx.mock
async def test_workday_search_and_details(client):
    ref = CompanyRef(
        "workday",
        "chipco/External",
        "ChipCo",
        config={"host": "chipco.wd5.myworkdayjobs.com", "tenant": "chipco", "site": "External"},
        search_terms=["software engineer"],
    )
    route = respx.post("https://chipco.wd5.myworkdayjobs.com/wday/cxs/chipco/External/jobs").respond(
        json=load_json("workday_jobs.json")
    )
    jobs = await workday.fetch_jobs(client, ref)
    body = json.loads(route.calls[0].request.content)
    assert body == {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "software engineer"}
    assert len(jobs) == 2
    job = jobs[0]
    assert (
        job.url
        == "https://chipco.wd5.myworkdayjobs.com/External/job/US-CA-Santa-Clara/Software-Engineer_JR1990001"
    )
    assert job.needs_details
    respx.get(job.extra["detail_url"]).respond(json=load_json("workday_detail.json"))
    await workday.fetch_details(client, job)
    assert "Write drivers in C++." in job.description
    assert job.employment_type == "Full time"
    assert job.posted_at == datetime(2026, 10, 25)


async def test_workday_requires_config(client):
    with pytest.raises(SourceError, match="missing host"):
        await workday.fetch_jobs(client, CompanyRef("workday", "x/y"))


@respx.mock
async def test_recruitee(client):
    respx.get("https://vandelay.recruitee.com/api/offers/").respond(json=load_json("recruitee_offers.json"))
    jobs = await recruitee.fetch_jobs(client, CompanyRef("recruitee", "vandelay", "Vandelay"))
    job = jobs[0]
    assert job.workplace == "hybrid"
    assert (job.salary_min, job.salary_max, job.salary_currency, job.salary_period) == (
        60000,
        75000,
        "EUR",
        "year",
    )
    assert job.apply_url.endswith("/c/new")
    assert "• Terraform" in job.description


@respx.mock
async def test_adzuna(client):
    env = Env(adzuna_app_id="id", adzuna_app_key="key")
    route = respx.get("https://api.adzuna.com/v1/api/jobs/us/search/1").respond(
        json=load_json("adzuna_search.json")
    )
    jobs = await adzuna.search(client, SearchRef("backend engineer", "New York", hours_old=48), env)
    params = route.calls[0].request.url.params
    assert (
        params["what"] == "backend engineer"
        and params["where"] == "New York"
        and params["max_days_old"] == "2"
    )
    job = jobs[0]
    assert job.title == "Backend Engineer"  # <strong> highlight tags removed
    assert job.company == "Acme"
    assert (job.salary_min, job.salary_currency) == (140000, "USD")


async def test_adzuna_needs_keys(client):
    with pytest.raises(SourceError, match="ADZUNA_APP_ID"):
        await adzuna.search(client, SearchRef("x"), Env())


@respx.mock
async def test_jsearch_prefers_direct_apply_link(client):
    env = Env(jsearch_api_key="k")
    route = respx.get("https://jsearch.p.rapidapi.com/search").respond(json=load_json("jsearch_search.json"))
    jobs = await jsearch.search(client, SearchRef("backend engineer", "New York", hours_old=72), env)
    request = route.calls[0].request
    assert request.headers["X-RapidAPI-Key"] == "k"
    assert request.url.params["query"] == "backend engineer in New York"
    assert request.url.params["date_posted"] == "3days"
    job = jobs[0]
    assert job.apply_url == "https://boards.greenhouse.io/initrode/jobs/555"
    assert job.url == "https://www.linkedin.com/jobs/view/1234567890"
    assert (job.salary_min, job.salary_max, job.salary_period) == (150000, 180000, "year")


@respx.mock
async def test_remotive(client):
    respx.get("https://remotive.com/api/remote-jobs").respond(json=load_json("remotive.json"))
    jobs = await remotive.search(client, SearchRef("python"), Env())
    job = jobs[0]
    assert job.location == "Remote (USA)" and job.workplace == "remote"
    assert (job.salary_min, job.salary_max) == (130000, 160000)


@respx.mock
async def test_remoteok_filters_by_keywords(client):
    respx.get("https://remoteok.com/api").respond(json=load_json("remoteok.json"))
    jobs = await remoteok.search(client, SearchRef("python backend"), Env())
    assert [j.title for j in jobs] == ["Python Backend Engineer"]
    assert jobs[0].salary_max == 140000


@respx.mock
async def test_request_json_retries_on_429(client, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("jobbot.sources.base.asyncio.sleep", no_sleep)
    route = respx.get("https://api.example.test/x")
    route.side_effect = [httpx.Response(429), httpx.Response(200, json={"ok": True})]
    assert await request_json(client, "https://api.example.test/x") == {"ok": True}
    assert route.call_count == 2


def test_jobspy_row_mapping():
    row = {
        "id": "li-4012345",
        "site": "linkedin",
        "job_url": "https://www.linkedin.com/jobs/view/4012345",
        "job_url_direct": "https://jobs.lever.co/globex/7f3c1a52-0f6e-4c55-9d6a-1b2c3d4e5f60",
        "title": "Data Engineer",
        "company": "Globex",
        "location": "Austin, TX, US",
        "date_posted": datetime(2026, 10, 24).date(),
        "job_type": "fulltime",
        "interval": "yearly",
        "min_amount": 120000.0,
        "max_amount": float("nan"),
        "currency": "USD",
        "is_remote": False,
        "description": "**About the role**\n\n\\- Build pipelines\n\\- 2\\+ years of Spark",
        "work_from_home_type": "Hybrid",
        "job_level": "mid-senior level",
        "emails": math.nan,
    }
    posting = jobspy_source.parse_row(row)
    assert posting.source == "linkedin"
    assert posting.apply_url.startswith("https://jobs.lever.co/")
    assert posting.salary_min == 120000 and posting.salary_max is None and posting.salary_period == "year"
    assert posting.workplace == "hybrid"
    assert posting.posted_at == datetime(2026, 10, 24)
    assert "About the role" in posting.description and "2+ years of Spark" in posting.description
    assert jobspy_source.parse_row({"site": "indeed", "title": "", "job_url": "x"}) is None


def test_sanitize_posting_drops_unsafe_urls():
    from jobbot.sources.base import JobPosting

    bad = JobPosting(source="x", source_id="1", title="T", company="C", url="javascript:alert(1)")
    assert not sanitize_posting(bad)
    ok = JobPosting(
        source="x",
        source_id="1",
        title=" Engineer\n ",
        company=" C ",
        url=" https://a.test/1 ",
        apply_url="data:x",
    )
    assert sanitize_posting(ok)
    assert (
        ok.url == "https://a.test/1" and ok.apply_url is None and ok.title == "Engineer" and ok.company == "C"
    )


@pytest.mark.parametrize(
    "url, ats, slug",
    [
        ("https://boards.greenhouse.io/stripe", "greenhouse", "stripe"),
        ("https://job-boards.greenhouse.io/stripe/jobs/123", "greenhouse", "stripe"),
        ("https://boards.greenhouse.io/embed/job_board?for=acme&b=x", "greenhouse", "acme"),
        ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", "greenhouse", "acme"),
        ("https://jobs.lever.co/netflix/abc-123", "lever", "netflix"),
        ("jobs.lever.co/netflix", "lever", "netflix"),
        ("https://jobs.ashbyhq.com/ramp", "ashby", "ramp"),
        ("https://jobs.smartrecruiters.com/Visa/744000", "smartrecruiters", "Visa"),
        ("https://careers.smartrecruiters.com/Bosch", "smartrecruiters", "Bosch"),
        ("https://apply.workable.com/huggingface/", "workable", "huggingface"),
        (
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite",
            "workday",
            "nvidia/NVIDIAExternalCareerSite",
        ),
        (
            "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US/X_JR1",
            "workday",
            "nvidia/NVIDIAExternalCareerSite",
        ),
        ("https://wd1.myworkdaysite.com/en-US/recruiting/acme/Careers", "workday", "acme/Careers"),
        ("https://vandelay.recruitee.com/", "recruitee", "vandelay"),
    ],
)
def test_parse_ats_url(url, ats, slug):
    ref = parse_ats_url(url)
    assert ref is not None and (ref.ats, ref.slug) == (ats, slug)


def test_parse_ats_url_extras():
    eu = parse_ats_url("https://jobs.eu.lever.co/acme")
    assert eu.config == {"region": "eu"}
    wd = parse_ats_url("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")
    assert wd.config == {
        "host": "nvidia.wd5.myworkdayjobs.com",
        "tenant": "nvidia",
        "site": "NVIDIAExternalCareerSite",
    }
    assert parse_ats_url("https://example.com/careers") is None
    assert parse_ats_url("https://apply.workable.com/j/ABC123") is None


def test_find_ats_in_html():
    html = """
      <a href="https://jobs.lever.co/acme/1">Role 1</a>
      <a href="https://jobs.lever.co/acme/2">Role 2</a>
      <script src="https://boards.greenhouse.io/embed/job_board/js?for=other"></script>
    """
    ref = find_ats_in_html(html)
    assert (ref.ats, ref.slug) == ("lever", "acme")
    assert find_ats_in_html("<script>Grnhse.Settings = {boardToken: 'widgets'}</script>").slug == "widgets"
    assert find_ats_in_html("<p>No jobs</p>") is None


@pytest.mark.parametrize(
    "url, ats",
    [
        ("https://job-boards.greenhouse.io/acme/jobs/1", "greenhouse"),
        ("https://acme.com/careers?gh_jid=123", "greenhouse"),
        ("https://acme.wd1.myworkdayjobs.com/x", "workday"),
        ("https://careers-acme.icims.com/jobs/1", "icims"),
        ("https://www.linkedin.com/jobs/view/1", "linkedin"),
        ("https://example.com/apply", None),
        (None, None),
    ],
)
def test_ats_for_url(url, ats):
    assert ats_for_url(url) == ats
