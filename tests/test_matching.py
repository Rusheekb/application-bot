"""Text extraction, hard filters and the heuristic score."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from jobbot.matching import (
    heuristic_score,
    hidden_reason,
    is_relevant,
    mentions_no_sponsorship,
    relevance_terms,
    requires_clearance,
)
from jobbot.models import Job
from jobbot.schemas import Filters, Profile
from jobbot.text import (
    annualize,
    detect_seniority,
    detect_workplace,
    extract_years_required,
    format_salary,
    html_to_text,
    job_fingerprint,
    keyword_in_text,
    parse_datetime,
    parse_relative_posted,
    parse_salary,
    term_matches,
)

NOW = datetime(2026, 10, 25, 12, 0)


def make_job(**overrides) -> Job:
    data = dict(
        title="Backend Engineer",
        company="Acme",
        location="New York, NY",
        workplace=None,
        description="Python, PostgreSQL and AWS. 3+ years of experience.",
        posted_at=NOW - timedelta(days=2),
        years_required=3,
        seniority=None,
        salary_min=None,
        salary_max=None,
        salary_currency=None,
        salary_period=None,
    )
    data.update(overrides)
    return Job(**data)


PROFILE = Profile(
    first_name="Jane",
    years_experience=4,
    target_titles=["Backend Engineer", "Software Engineer"],
    skills=["Python", "PostgreSQL", "AWS", "Kubernetes", "Go"],
    workplace_preferences=["remote", "hybrid", "onsite"],
)


@pytest.mark.parametrize(
    "text, years",
    [
        ("5+ years of experience in Python", 5),
        ("3-5 years of professional experience", 3),
        ("Minimum of seven years experience", 7),
        ("At least 2 years experience; 5+ years experience preferred", 2),
        ("five (5) years of relevant experience", 5),
        ("We have been in business for 20 years. Experience with Go.", None),
        ("Experience: 10+ yrs building systems", 10),
        ("", None),
    ],
)
def test_extract_years_required(text, years):
    assert extract_years_required(text) == years


@pytest.mark.parametrize(
    "title, level",
    [
        ("Senior Software Engineer", "senior"),
        ("Sr. Data Scientist", "senior"),
        ("Software Engineer II", "mid"),
        ("Software Engineer I", "entry"),
        ("Junior Developer", "entry"),
        ("Staff Engineer", "staff"),
        ("Principal Architect", "principal"),
        ("Head of Data", "director"),
        ("Software Engineering Intern", "intern"),
        ("Product Manager", None),
    ],
)
def test_detect_seniority(title, level):
    assert detect_seniority(title) == level


@pytest.mark.parametrize(
    "text, expected",
    [
        ("The salary range is $120,000 - $150,000 per year.", (120000, 150000, "USD", "year")),
        ("Pay: $55/hr - $70/hr", (55, 70, "USD", "hour")),
        ("$140k–$180k + equity", (140000, 180000, "USD", "year")),
        ("€60.000 - €75.000", (60000, 75000, "EUR", "year")),
        ("£40,000 to £50,000", (40000, 50000, "GBP", "year")),
        ("Bonus of $200 - $500", None),
        ("No salary here", None),
    ],
)
def test_parse_salary(text, expected):
    assert parse_salary(text) == expected


def test_salary_helpers():
    assert annualize(50, "hour") == 104000
    assert format_salary(120000, 150000, "USD", "year") == "$120k–$150k"
    assert format_salary(55, 70, "USD", "hour") == "$55–$70/hr"
    assert format_salary(None, None, None, None) == ""


def test_html_to_text_keeps_structure():
    html = "&lt;h3&gt;About&lt;/h3&gt;&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;&lt;p&gt;One&lt;/p&gt;&lt;/li&gt;&lt;li&gt;Two&lt;/li&gt;&lt;/ul&gt;"
    assert html_to_text(html) == "About\n\nHello & welcome\n\n• One\n• Two"
    assert html_to_text("<script>alert(1)</script><p>Text</p>") == "Text"
    assert html_to_text(None) == ""


def test_title_matching():
    assert term_matches("software engineer", "Senior Software Engineer, Payments")
    assert term_matches("full stack", "Full-Stack Developer")
    assert term_matches("backend engineer", "Back End Engineer")
    assert term_matches("ml engineer", "Machine Learning Engineer")
    assert term_matches("software engineer", "Software Engineering Manager")
    assert not term_matches("data scientist", "Data Engineer")
    assert not term_matches("", "Anything")
    assert is_relevant("Anything", [])
    assert not is_relevant("Account Executive", ["software engineer"])


def test_keyword_in_text_handles_symbols():
    assert keyword_in_text("C++", "Experience with C++ and Go")
    assert keyword_in_text(".NET", "C# and .NET")
    assert not keyword_in_text("C", "C# and .NET")
    assert not keyword_in_text("Go", "Good communication")
    assert keyword_in_text("Node.js", "We use node.js")


def test_fingerprint_matches_across_sources():
    a = job_fingerprint("Stripe, Inc.", "Sr. Software Engineer (Remote)", "Remote - US")
    b = job_fingerprint("Stripe", "Senior Software Engineer", "Remote")
    c = job_fingerprint("Stripe", "Senior Software Engineer", "New York, NY")
    assert a == b != c
    assert job_fingerprint("Acme", "Engineer", "New York City, NY") == job_fingerprint(
        "ACME LLC", "Engineer", "NYC"
    )


def test_dates():
    assert parse_datetime("2024-05-01T12:00:00-04:00") == datetime(2024, 5, 1, 16, 0)
    assert parse_datetime(1700000000000) == datetime(2023, 11, 14, 22, 13, 20)
    assert parse_datetime("2024-05-01 12:00:00 UTC") == datetime(2024, 5, 1, 12, 0)
    assert parse_datetime("not a date") is None
    assert parse_relative_posted("Posted 30+ Days Ago", NOW) == NOW - timedelta(days=30)
    assert parse_relative_posted("Posted Yesterday", NOW) == NOW - timedelta(days=1)


def test_detect_workplace():
    assert detect_workplace("Engineer", "Remote - US") == "remote"
    assert detect_workplace("Engineer", "New York, NY", "This is a hybrid role") == "hybrid"
    assert detect_workplace("Engineer (On-site)", "Austin") == "onsite"
    assert detect_workplace("Engineer", "Austin") is None


def test_sponsorship_and_clearance_detection():
    assert mentions_no_sponsorship("We are unable to sponsor visas for this role.")
    assert mentions_no_sponsorship(
        "Must be authorized to work in the US without the need for current or future visa sponsorship."
    )
    assert not mentions_no_sponsorship("Visa sponsorship available.")
    assert requires_clearance("Active Secret clearance required")
    assert not requires_clearance("Ability to obtain a security clearance")


def test_relevance_terms_dedupes():
    filters = Filters(title_include=["Data Engineer", "backend engineer"])
    assert relevance_terms(filters, PROFILE, ["Backend Engineers", "Platform Engineer"]) == [
        "Data Engineer",
        "backend engineer",
        "Software Engineer",
        "Platform Engineer",
    ]


@pytest.mark.parametrize(
    "job_kwargs, filters_kwargs, profile_kwargs, reason",
    [
        ({}, {}, {}, None),
        ({"title": "Senior Backend Engineer"}, {"title_exclude": ["senior"]}, {}, "Title contains “senior”"),
        ({"title": "Sr. Backend Engineer"}, {"title_exclude": ["senior"]}, {}, "Title contains “senior”"),
        (
            {"company": "Acme, Inc."},
            {"company_exclude": ["acme"]},
            {},
            "Company is on your exclude list (Acme, Inc.)",
        ),
        ({"years_required": 8}, {"max_years_gap": 2}, {}, "Asks for 8+ years (you have 4)"),
        ({"years_required": 6}, {"max_years_gap": 2}, {}, None),
        (
            {"location": "London, UK"},
            {"locations": ["New York"]},
            {},
            "Location not in your list (London, UK)",
        ),
        ({"location": "Remote", "workplace": "remote"}, {"locations": ["New York"]}, {}, None),
        (
            {"location": "Remote", "workplace": "remote"},
            {"include_remote": False},
            {},
            "Remote roles are turned off in your filters",
        ),
        (
            {"location": "Bangalore, India"},
            {"location_exclude": ["India"]},
            {},
            "Location excluded (Bangalore, India)",
        ),
        (
            {"salary_min": 90000, "salary_max": 110000, "salary_period": "year"},
            {"min_salary": 120000},
            {},
            "Pay ($90k–$110k) is below your minimum",
        ),
        ({"salary_min": 60, "salary_max": 70, "salary_period": "hour"}, {"min_salary": 120000}, {}, None),
        (
            {"description": "We cannot sponsor visas."},
            {},
            {"requires_sponsorship": True},
            "Says it can't sponsor work visas",
        ),
        ({"description": "We cannot sponsor visas."}, {}, {}, None),
        (
            {"description": "Active TS/SCI clearance required."},
            {},
            {},
            "Requires an active security clearance",
        ),
        ({"description": "Active TS/SCI clearance required."}, {}, {"security_clearance": "TS/SCI"}, None),
        ({"posted_at": NOW - timedelta(days=45)}, {"max_age_days": 30}, {}, "Posted 45 days ago"),
        (
            {"description": "commission only role"},
            {"description_exclude": ["commission only"]},
            {},
            "Description mentions “commission only”",
        ),
    ],
)
def test_hidden_reason(job_kwargs, filters_kwargs, profile_kwargs, reason):
    profile = PROFILE.model_copy(update=profile_kwargs)
    assert hidden_reason(make_job(**job_kwargs), profile, Filters(**filters_kwargs), NOW) == reason


def test_heuristic_score_rewards_fit():
    good, breakdown = heuristic_score(make_job(), PROFILE, [], NOW)
    assert breakdown["title"] == 30
    assert set(breakdown["matched_skills"]) == {"Python", "PostgreSQL", "AWS"}
    stretch, _ = heuristic_score(
        make_job(years_required=9, title="Staff Backend Engineer", seniority="staff"), PROFILE, [], NOW
    )
    stale, _ = heuristic_score(make_job(posted_at=NOW - timedelta(days=40)), PROFILE, [], NOW)
    unrelated, _ = heuristic_score(
        make_job(title="Account Executive", description="Sell software."), PROFILE, [], NOW
    )
    assert good > stale > unrelated
    assert good > stretch
    assert 0 <= unrelated <= good <= 100


def test_heuristic_score_without_profile_is_neutral():
    score, breakdown = heuristic_score(make_job(), Profile(), [], NOW)
    assert 40 <= score <= 70
    assert "Add skills to your profile for better matching" in breakdown["notes"]
